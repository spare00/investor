"""Intraday operations facade — monitor, intents for reduce/close, orchestration."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.brokers.models import IntentStatus, IntentType
from app.core.config import Settings, get_settings
from app.execution.safety_controls import TradingControls, trading_controls
from app.intraday.agents import IntradayAgentService
from app.intraday.broker_updates import BrokerUpdateProcessor
from app.intraday.closing import ClosingService
from app.intraday.events import IntradayEventBus
from app.intraday.exits import ExitPolicyEngine, StopKind
from app.intraday.modes import ModeCapabilities, resolve_mode
from app.intraday.monitor import PositionMonitor
from app.intraday.posttrade import PostTradeReviewService
from app.intraday.recovery import IntradayRecoveryService
from app.intraday.risk import DynamicRiskRevalidator
from app.intraday.settlement import SettlementService
from app.models import OrderIntent, PositionLifecycle, PositionSnapshotRecord


class IntradayService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        controls: TradingControls | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.controls = controls or trading_controls
        self.bus = IntradayEventBus(session, settings=self.settings)
        self.monitor = PositionMonitor(session, settings=self.settings)
        self.risk = DynamicRiskRevalidator(session, settings=self.settings)
        self.exits = ExitPolicyEngine(session, settings=self.settings)
        self.closing = ClosingService(session, settings=self.settings)
        self.settlement = SettlementService(session, settings=self.settings)
        self.posttrade = PostTradeReviewService(session)
        self.agents = IntradayAgentService(session, settings=self.settings, controls=self.controls)
        self.recovery = IntradayRecoveryService(session, settings=self.settings)
        self.broker_updates = BrokerUpdateProcessor(session, settings=self.settings)

    def status(self) -> dict[str, Any]:
        snap = self.controls.snapshot()
        mode = resolve_mode(
            self.settings,
            emergency=snap.state.value == "emergency_stop",
            paused=snap.state.value == "paused",
        )
        caps = ModeCapabilities(mode)
        return {
            "mode": mode.value,
            "trading_state": snap.state.value,
            "can_analyze": caps.can_analyze,
            "can_create_intent": caps.can_create_intent,
            "can_submit": caps.can_submit and self.settings.enable_broker_orders,
            "enable_live_trading": self.settings.enable_live_trading,
            "enable_automated_execution": self.settings.enable_automated_execution,
            "broker_streaming_enabled": self.settings.broker_streaming_enabled,
            "broker_polling_fallback_enabled": self.settings.broker_polling_fallback_enabled,
            "auto_execute_hard_stops": self.settings.auto_execute_hard_stops,
        }

    async def monitor_all(self, prices: dict[str, float] | None = None) -> list[dict[str, Any]]:
        prices = prices or {}
        out: list[dict[str, Any]] = []
        for lc in await self.monitor.list_lifecycles():
            result = await self.monitor.evaluate(
                lc,
                current_price=prices.get(lc.symbol),
                equity=self.settings.starting_cash,
            )
            risk = await self.risk.evaluate(
                lc,
                equity=self.settings.starting_cash,
                daily_pnl_pct=0.0,
                drawdown_pct=0.0,
                price=prices.get(lc.symbol),
            )
            stop = await self.exits.check_stop(
                lc, price=float(prices.get(lc.symbol) or lc.current_price or 0), kind=StopKind.FIXED_PRICE
            )
            out.append(
                {
                    "position_id": str(lc.id),
                    "symbol": lc.symbol,
                    "monitor": {"verdict": result.verdict, "reasons": result.reasons},
                    "risk": {"status": risk.status, "reasons": risk.reasons},
                    "stop": {"triggered": stop.triggered, "status": stop.status},
                }
            )
            # Hard stop → exit intent draft (never direct broker)
            if stop.triggered:
                await self._exit_intent(lc, reason="hard_stop", qty=float(lc.quantity or 0))
        return out

    async def _exit_intent(
        self, lc: PositionLifecycle, *, reason: str, qty: float, reduce_fraction: float | None = None
    ) -> OrderIntent | None:
        mode = resolve_mode(self.settings, emergency=self.controls.snapshot().state.value == "emergency_stop")
        caps = ModeCapabilities(mode)
        if not caps.can_create_intent and mode.value != "OBSERVE_ONLY":
            return None
        # OBSERVE_ONLY: draft metadata only on lifecycle
        exit_qty = qty if reduce_fraction is None else max(0.0, qty * reduce_fraction)
        if exit_qty <= 0:
            return None
        if caps.intents_are_draft_only or mode.value == "OBSERVE_ONLY":
            meta = dict(lc.metadata_json or {})
            meta["exit_draft"] = {"reason": reason, "qty": exit_qty, "at": datetime.now(UTC).isoformat()}
            lc.metadata_json = meta
            await self.session.flush()
            return None
        intent = OrderIntent(
            id=uuid4(),
            decision_id=lc.decision_id,
            symbol=lc.symbol,
            intent_type=IntentType.CLOSE_LONG.value if reduce_fraction is None else IntentType.REDUCE_LONG.value,
            side="sell",
            quantity=exit_qty,
            entry_price=lc.current_price,
            stop_price=lc.stop_price,
            status=IntentStatus.CREATED.value if caps.can_approve else "DRAFT",
            thesis=reason,
            exit_policy=dict(lc.exit_policy or {}),
            metadata_json={"source": "intraday", "reason": reason, "lifecycle_id": str(lc.id)},
        )
        if not self.settings.auto_execute_hard_stops and reason == "hard_stop":
            intent.status = IntentStatus.PENDING_APPROVAL.value
        self.session.add(intent)
        lc.status = "PENDING_CLOSE" if reduce_fraction is None else "REDUCING"
        await self.session.flush()
        return intent

    async def reduce_position(self, position_id: UUID, *, fraction: float = 0.5) -> dict[str, Any]:
        lc = await self.session.get(PositionLifecycle, position_id)
        if lc is None:
            raise ValueError("position_not_found")
        intent = await self._exit_intent(lc, reason="operator_reduce", qty=float(lc.quantity or 0), reduce_fraction=fraction)
        return {
            "position_id": str(position_id),
            "intent_id": None if intent is None else str(intent.id),
            "broker_orders_submitted": False,
            "path": "intent_risk_approval_execution",
        }

    async def close_position(self, position_id: UUID) -> dict[str, Any]:
        lc = await self.session.get(PositionLifecycle, position_id)
        if lc is None:
            raise ValueError("position_not_found")
        intent = await self._exit_intent(lc, reason="operator_close", qty=float(lc.quantity or 0))
        return {
            "position_id": str(position_id),
            "intent_id": None if intent is None else str(intent.id),
            "broker_orders_submitted": False,
            "path": "intent_risk_approval_execution",
        }

    async def update_exit_policy(
        self, position_id: UUID, *, stop_price: float | None = None, take_profit_targets: list | None = None
    ) -> dict[str, Any]:
        lc = await self.session.get(PositionLifecycle, position_id)
        if lc is None:
            raise ValueError("position_not_found")
        if stop_price is not None and lc.stop_price is not None:
            lc.stop_price = self.exits.adjust_stop(current_stop=float(lc.stop_price), proposed_stop=stop_price)
        elif stop_price is not None:
            lc.stop_price = stop_price
        if take_profit_targets is not None:
            lc.take_profit_targets = take_profit_targets
        policy = dict(lc.exit_policy or {})
        policy["stop_loss"] = lc.stop_price
        policy["take_profit_targets"] = lc.take_profit_targets
        lc.exit_policy = policy
        await self.session.flush()
        return {"position_id": str(position_id), "exit_policy": policy}

    async def snapshots(self, position_id: UUID) -> list[dict[str, Any]]:
        rows = list(
            (
                await self.session.execute(
                    select(PositionSnapshotRecord)
                    .where(PositionSnapshotRecord.position_lifecycle_id == position_id)
                    .order_by(PositionSnapshotRecord.as_of.desc())
                    .limit(50)
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "snapshot_id": str(r.id),
                "symbol": r.symbol,
                "quantity": r.quantity,
                "current_price": r.current_price,
                "unrealized_pl": r.unrealized_pl,
                "as_of": r.as_of.isoformat() if r.as_of else None,
            }
            for r in rows
        ]
