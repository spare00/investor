"""Stop-loss, take-profit, invalidation state machines."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.intraday.events import IntradayEventBus
from app.models import PositionLifecycle, StopEvent, TakeProfitEvent


class StopKind(StrEnum):
    FIXED_PRICE = "FIXED_PRICE"
    PERCENTAGE = "PERCENTAGE"
    ATR_BASED = "ATR_BASED"
    TIME_BASED = "TIME_BASED"


class StopStatus(StrEnum):
    ACTIVE = "ACTIVE"
    TRIGGERED = "TRIGGERED"
    ORDER_PENDING = "ORDER_PENDING"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"


@dataclass(slots=True)
class StopCheckResult:
    triggered: bool
    kind: str
    stop_price: float | None
    status: str
    reasons: list[str]


@dataclass(slots=True)
class TakeProfitCheckResult:
    triggered: bool
    target_index: int | None
    quantity_to_exit: float
    reasons: list[str]


class ExitPolicyEngine:
    def __init__(self, session: AsyncSession, *, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.bus = IntradayEventBus(session, settings=self.settings)

    def compute_stop_price(
        self,
        *,
        kind: StopKind,
        entry: float,
        side: str = "long",
        fixed: float | None = None,
        pct: float | None = None,
        atr: float | None = None,
        atr_mult: float = 2.0,
    ) -> float | None:
        if kind == StopKind.FIXED_PRICE:
            return fixed
        if kind == StopKind.PERCENTAGE and pct is not None:
            if side == "long":
                return entry * (1 - pct / 100.0)
            return entry * (1 + pct / 100.0)
        if kind == StopKind.ATR_BASED and atr is not None:
            if side == "long":
                return entry - atr * atr_mult
            return entry + atr * atr_mult
        return fixed

    def adjust_stop(
        self,
        *,
        current_stop: float,
        proposed_stop: float,
        side: str = "long",
    ) -> float:
        """Only allow tightening by default; block widening."""
        if side == "long":
            if proposed_stop < current_stop and not self.settings.allow_stop_widening:
                return current_stop  # widening blocked
            if proposed_stop > current_stop and self.settings.allow_stop_tightening:
                return proposed_stop
            if proposed_stop < current_stop and self.settings.allow_stop_widening:
                return proposed_stop
            return current_stop
        # short
        if proposed_stop > current_stop and not self.settings.allow_stop_widening:
            return current_stop
        if proposed_stop < current_stop and self.settings.allow_stop_tightening:
            return proposed_stop
        return current_stop

    async def check_stop(
        self,
        lifecycle: PositionLifecycle,
        *,
        price: float,
        quote_stale: bool = False,
        kind: StopKind = StopKind.FIXED_PRICE,
    ) -> StopCheckResult:
        stop = lifecycle.stop_price
        reasons: list[str] = []
        if stop is None:
            return StopCheckResult(False, kind.value, None, StopStatus.ACTIVE.value, ["no_stop"])
        # Conservative: stale quote does not auto-trigger unless clearly through
        trigger_price = float(stop)
        if quote_stale:
            reasons.append("stale_quote_conservative")
            # require deeper breach (+0.5%)
            trigger_price = float(stop) * 0.995
        triggered = price <= trigger_price and float(lifecycle.quantity or 0) > 0
        status = StopStatus.TRIGGERED if triggered else StopStatus.ACTIVE
        if triggered:
            self.session.add(
                StopEvent(
                    id=uuid4(),
                    position_lifecycle_id=lifecycle.id,
                    kind=kind.value,
                    status=status.value,
                    stop_price=float(stop),
                    trigger_price=price,
                    payload={"reasons": reasons},
                )
            )
            lifecycle.stop_status = status.value
            await self.session.flush()
        return StopCheckResult(triggered, kind.value, float(stop), status.value, reasons)

    async def check_take_profit(
        self,
        lifecycle: PositionLifecycle,
        *,
        price: float,
    ) -> TakeProfitCheckResult:
        targets = list(lifecycle.take_profit_targets or [])
        qty = float(lifecycle.quantity or 0)
        filled_targets = set(lifecycle.filled_take_profit_indices or [])
        if not targets or qty <= 0:
            return TakeProfitCheckResult(False, None, 0.0, ["no_targets"])
        for idx, target in enumerate(targets):
            if idx in filled_targets:
                continue
            level = float(target.get("price") if isinstance(target, dict) else target)
            frac = float(target.get("fraction", 0.25) if isinstance(target, dict) else 0.25)
            if price >= level:
                exit_qty = min(qty, qty * frac)
                # fractional rounding
                if not self.settings.enable_short_selling:
                    exit_qty = round(exit_qty, 6)
                if exit_qty <= 0:
                    continue
                self.session.add(
                    TakeProfitEvent(
                        id=uuid4(),
                        position_lifecycle_id=lifecycle.id,
                        target_index=idx,
                        target_price=level,
                        quantity=exit_qty,
                        status="TRIGGERED",
                        payload={"fraction": frac},
                    )
                )
                filled = list(filled_targets)
                filled.append(idx)
                lifecycle.filled_take_profit_indices = filled
                lifecycle.take_profit_state = f"target_{idx}_triggered"
                await self.session.flush()
                return TakeProfitCheckResult(True, idx, exit_qty, ["target_hit"])
        return TakeProfitCheckResult(False, None, 0.0, ["no_hit"])

    def evaluate_invalidation(
        self,
        *,
        thesis_status: str,
        hard_news: bool = False,
        regime_break: bool = False,
        data_unreliable: bool = False,
    ) -> str:
        if data_unreliable:
            return "UNKNOWN_DUE_TO_DATA"
        if thesis_status.upper() == "INVALIDATED" or hard_news or regime_break:
            return "CONFIRMED"
        if thesis_status.upper() == "WEAKENED":
            return "POSSIBLE"
        return "NOT_TRIGGERED"
