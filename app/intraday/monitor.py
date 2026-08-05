"""Position Monitor — observe only; never submits broker orders."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.intraday.events import IntradayEventBus
from app.models import PositionLifecycle, PositionSnapshotRecord


class MonitorVerdict(str):
    pass


HEALTHY = "HEALTHY"
WATCH = "WATCH"
RISK_REVIEW_REQUIRED = "RISK_REVIEW_REQUIRED"
ANALYSIS_REQUIRED = "ANALYSIS_REQUIRED"
EXIT_INTENT_REQUIRED = "EXIT_INTENT_REQUIRED"
RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
EMERGENCY_ACTION_REQUIRED = "EMERGENCY_ACTION_REQUIRED"


@dataclass(slots=True)
class MonitorResult:
    verdict: str
    symbol: str
    reasons: list[str] = field(default_factory=list)
    snapshot_id: str | None = None


class PositionMonitor:
    def __init__(self, session: AsyncSession, *, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.bus = IntradayEventBus(session, settings=self.settings)

    async def list_lifecycles(self) -> list[PositionLifecycle]:
        return list(
            (
                await self.session.execute(
                    select(PositionLifecycle).where(
                        PositionLifecycle.status.in_(
                            ["PENDING_OPEN", "OPEN", "ADDING", "REDUCING", "PENDING_CLOSE"]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )

    async def evaluate(
        self,
        lifecycle: PositionLifecycle,
        *,
        current_price: float | None,
        quote_age_seconds: float | None = 0.0,
        spread_bps: float | None = 10.0,
        halted: bool = False,
        broker_qty: float | None = None,
        equity: float = 25_000.0,
        daily_pnl_pct: float = 0.0,
        drawdown_pct: float = 0.0,
    ) -> MonitorResult:
        reasons: list[str] = []
        verdict = HEALTHY
        qty = float(lifecycle.quantity or 0)
        entry = float(lifecycle.average_entry_price or 0)
        price = float(current_price or lifecycle.current_price or entry or 0)
        stop = lifecycle.stop_price
        now = datetime.now(UTC)

        if halted:
            verdict = RISK_REVIEW_REQUIRED
            reasons.append("trading_halt")
        if quote_age_seconds is not None and quote_age_seconds > self.settings.latest_quote_max_age_seconds * 20:
            verdict = RISK_REVIEW_REQUIRED
            reasons.append("stale_quote")
        if spread_bps is not None and spread_bps > self.settings.max_order_spread_bps:
            verdict = WATCH
            reasons.append("spread_wide")
        if broker_qty is not None and abs(broker_qty - qty) > 1e-6:
            verdict = RECONCILIATION_REQUIRED
            reasons.append("broker_qty_drift")
        if lifecycle.reconciliation_required:
            verdict = RECONCILIATION_REQUIRED
            reasons.append("flagged_reconciliation")

        # Stop proximity / trigger
        if stop is not None and price > 0 and qty > 0:
            if price <= float(stop):
                verdict = EXIT_INTENT_REQUIRED
                reasons.append("stop_triggered")
                await self.bus.publish(
                    event_type="STOP_TRIGGERED",
                    source="position_monitor",
                    symbols=[lifecycle.symbol],
                    deduplication_key=f"stop:{lifecycle.id}:{now.strftime('%Y%m%d%H%M')}",
                    position_id=lifecycle.id,
                    requires_execution_review=True,
                    bypass_cooldown=True,
                    importance="critical",
                    payload={"stop": stop, "price": price},
                )
            else:
                dist = (price - float(stop)) / price * 100.0
                if dist < 1.0:
                    verdict = WATCH if verdict == HEALTHY else verdict
                    reasons.append("stop_proximity")

        # Take profit
        tp = lifecycle.take_profit_price
        if tp is not None and price >= float(tp) and qty > 0:
            verdict = EXIT_INTENT_REQUIRED if verdict != EMERGENCY_ACTION_REQUIRED else verdict
            reasons.append("take_profit_triggered")
            await self.bus.publish(
                event_type="TAKE_PROFIT_TRIGGERED",
                source="position_monitor",
                symbols=[lifecycle.symbol],
                deduplication_key=f"tp:{lifecycle.id}:{now.strftime('%Y%m%d%H%M')}",
                position_id=lifecycle.id,
                requires_execution_review=True,
                importance="high",
                payload={"take_profit": tp, "price": price},
            )

        # Max holding
        if lifecycle.opened_at and lifecycle.max_holding_minutes:
            opened = lifecycle.opened_at
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=UTC)
            held = (now - opened).total_seconds() / 60.0
            if held >= lifecycle.max_holding_minutes:
                verdict = EXIT_INTENT_REQUIRED if verdict not in {EMERGENCY_ACTION_REQUIRED} else verdict
                reasons.append("max_holding_time")
                await self.bus.publish(
                    event_type="MAX_HOLDING_TIME_REACHED",
                    source="position_monitor",
                    symbols=[lifecycle.symbol],
                    deduplication_key=f"hold:{lifecycle.id}",
                    position_id=lifecycle.id,
                    requires_risk_review=True,
                    importance="high",
                )

        # Protection order missing
        if not lifecycle.protection_submitted and qty > 0 and stop is not None:
            verdict = WATCH if verdict == HEALTHY else verdict
            reasons.append("protection_order_missing")

        # Portfolio risk
        if daily_pnl_pct <= -self.settings.daily_max_loss_pct:
            verdict = EMERGENCY_ACTION_REQUIRED
            reasons.append("daily_loss_limit")
        if drawdown_pct >= self.settings.max_drawdown_pct:
            verdict = EMERGENCY_ACTION_REQUIRED
            reasons.append("drawdown_limit")

        weight = (abs(qty) * price / equity * 100.0) if equity and price else 0.0
        if weight > self.settings.max_position_pct * 1.1:
            verdict = RISK_REVIEW_REQUIRED if verdict == HEALTHY else verdict
            reasons.append("position_concentration")

        snap = await self._snapshot(lifecycle, price=price, equity=equity)
        lifecycle.current_price = price
        lifecycle.unrealized_pl = (price - entry) * qty if entry else 0.0
        lifecycle.last_monitor_verdict = verdict
        await self.session.flush()
        return MonitorResult(verdict=verdict, symbol=lifecycle.symbol, reasons=reasons, snapshot_id=str(snap.id))

    async def _snapshot(self, lifecycle: PositionLifecycle, *, price: float, equity: float) -> PositionSnapshotRecord:
        qty = float(lifecycle.quantity or 0)
        entry = float(lifecycle.average_entry_price or 0)
        mv = qty * price
        upnl = (price - entry) * qty if entry else 0.0
        upnl_pct = ((price - entry) / entry * 100.0) if entry else 0.0
        weight = (abs(mv) / equity * 100.0) if equity else 0.0
        stop = lifecycle.stop_price
        dist = None
        if stop is not None and price > 0:
            dist = (price - float(stop)) / price * 100.0
        held = None
        if lifecycle.opened_at:
            opened = lifecycle.opened_at
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=UTC)
            held = (datetime.now(UTC) - opened).total_seconds() / 60.0
        snap = PositionSnapshotRecord(
            id=uuid4(),
            position_lifecycle_id=lifecycle.id,
            symbol=lifecycle.symbol,
            quantity=qty,
            market_value=mv,
            average_entry_price=entry or None,
            current_price=price,
            unrealized_pl=upnl,
            unrealized_pl_pct=upnl_pct,
            realized_pl=float(lifecycle.realized_pl or 0),
            portfolio_weight_pct=weight,
            stop_price=stop,
            distance_to_stop_pct=dist,
            take_profit_state=lifecycle.take_profit_state,
            holding_minutes=held,
            risk_amount=None,
            data_quality=1.0,
            as_of=datetime.now(UTC),
            source="position_monitor",
        )
        self.session.add(snap)
        await self.session.flush()
        return snap

    async def ensure_lifecycle_from_broker(
        self,
        *,
        symbol: str,
        quantity: float,
        avg_entry: float,
        decision_id: UUID | None = None,
        stop_price: float | None = None,
    ) -> PositionLifecycle:
        existing = (
            await self.session.execute(
                select(PositionLifecycle)
                .where(PositionLifecycle.symbol == symbol.upper())
                .where(PositionLifecycle.status.in_(["OPEN", "PENDING_OPEN", "ADDING", "REDUCING"]))
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing:
            existing.quantity = quantity
            existing.average_entry_price = avg_entry
            await self.session.flush()
            return existing
        row = PositionLifecycle(
            id=uuid4(),
            symbol=symbol.upper(),
            status="OPEN",
            quantity=quantity,
            average_entry_price=avg_entry,
            current_price=avg_entry,
            stop_price=stop_price,
            decision_id=decision_id,
            opened_at=datetime.now(UTC),
            overnight_allowed=False,
            closing_policy=self.settings.default_closing_policy,
            protection_submitted=False,
            max_holding_minutes=await self._default_max_holding(symbol),
            exit_policy={"stop_loss": stop_price},
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def _default_max_holding(self, symbol: str) -> int | None:
        """Prefer watchlist horizon policy; otherwise leave unset."""
        from sqlalchemy import select

        from app.models import WatchlistSymbol
        from app.universe.horizons import policy_for

        row = (
            await self.session.execute(
                select(WatchlistSymbol).where(WatchlistSymbol.symbol == symbol.upper()).limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        try:
            return int(policy_for(row.horizon).max_holding_minutes or 0) or None
        except ValueError:
            return None
