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
from app.market.venues import venue_for_symbol


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
            "auto_execute_hard_stops": self.settings.effective_auto_execute_hard_stops(),
        }

    async def monitor_all(
        self,
        prices: dict[str, float] | None = None,
        *,
        venue: str | None = None,
    ) -> list[dict[str, Any]]:
        if not self.settings.enable_intraday_monitoring:
            return [{"skipped": True, "reason": "enable_intraday_monitoring_false"}]
        prices = prices or {}
        from app.execution.position_manager import PositionManager

        port = await PositionManager(self.session, settings=self.settings).portfolio_state_input()
        equity = float(port.equity or self.settings.starting_cash)
        daily_pnl_pct = float(port.daily_pnl_pct or 0.0)
        drawdown_pct = float(port.drawdown_pct or 0.0)
        mode = resolve_mode(
            self.settings, emergency=self.controls.snapshot().state.value == "emergency_stop"
        )
        caps = ModeCapabilities(mode)
        book = str(venue).upper() if venue else None
        out: list[dict[str, Any]] = []
        for lc in await self.monitor.list_lifecycles(venue=book):
            stamped = await self.monitor.stamp_horizon_stop_if_missing(lc)
            result = await self.monitor.evaluate(
                lc,
                current_price=prices.get(lc.symbol),
                equity=equity,
                daily_pnl_pct=daily_pnl_pct,
                drawdown_pct=drawdown_pct,
            )
            risk = await self.risk.evaluate(
                lc,
                equity=equity,
                daily_pnl_pct=daily_pnl_pct,
                drawdown_pct=drawdown_pct,
                price=prices.get(lc.symbol),
            )
            stop = await self.exits.check_stop(
                lc, price=float(prices.get(lc.symbol) or lc.current_price or 0), kind=StopKind.FIXED_PRICE
            )
            entry: dict[str, Any] = {
                "position_id": str(lc.id),
                "symbol": lc.symbol,
                "monitor": {"verdict": result.verdict, "reasons": result.reasons},
                "risk": {"status": risk.status, "reasons": risk.reasons},
                "stop": {"triggered": stop.triggered, "status": stop.status},
            }
            if stamped is not None and lc.stop_price == stamped:
                entry["horizon_stop"] = stamped
            # Hard stop / max-holding / take-profit → exit intent (optional paper submit).
            protective_exit = stop.triggered or (
                result.verdict == "EXIT_INTENT_REQUIRED"
                and any(
                    r in {"stop_triggered", "max_holding_time", "take_profit_triggered"}
                    for r in (result.reasons or [])
                )
            )
            if protective_exit:
                reason = "hard_stop" if stop.triggered else str(
                    next(
                        (
                            r
                            for r in (result.reasons or [])
                            if r
                            in {
                                "max_holding_time",
                                "take_profit_triggered",
                                "stop_triggered",
                            }
                        ),
                        "monitor_exit",
                    )
                )
                if lc.status == "PENDING_CLOSE":
                    entry["exit_skipped"] = "already_pending_close"
                    intent = None
                else:
                    intent = await self._exit_intent(
                        lc, reason=reason, qty=float(lc.quantity or 0)
                    )
                if intent is not None:
                    entry["exit_intent_id"] = str(intent.id)
                # Unfilled ASX exits stay PENDING_CLOSE; still resubmit the daily key.
                submitted = 0
                if self._should_auto_submit_hard_stops(caps):
                    submitted = await self._submit_hard_stop(lc)
                    entry["orders_submitted"] = submitted
                    if submitted:
                        entry["notes"] = [
                            "hard_stop_orders_submitted"
                            if stop.triggered
                            else "protective_exit_orders_submitted"
                        ]
                elif intent is not None:
                    entry["notes"] = [
                        "hard_stop_intent_pending_submit"
                        if stop.triggered
                        else "protective_exit_intent_pending_submit"
                    ]
                if stop.triggered:
                    try:
                        from app.alerts.ops import emit_hard_stop_alert

                        await emit_hard_stop_alert(
                            self.session,
                            self.settings,
                            symbol=lc.symbol,
                            price=float(prices.get(lc.symbol) or lc.current_price or 0)
                            or None,
                            stop_price=float(lc.stop_price)
                            if lc.stop_price is not None
                            else None,
                            submitted=bool(submitted),
                            intent_id=str(intent.id) if intent is not None else None,
                        )
                    except Exception:  # noqa: BLE001
                        pass
            if result.verdict == "EMERGENCY_ACTION_REQUIRED":
                try:
                    from app.alerts.ops import emit_monitor_emergency_alert

                    await emit_monitor_emergency_alert(
                        self.session,
                        self.settings,
                        symbol=lc.symbol,
                        reasons=list(result.reasons or []),
                    )
                    entry["emergency_alert"] = True
                except Exception:  # noqa: BLE001
                    pass
            out.append(entry)
        return out

    def _should_auto_submit_hard_stops(self, caps: ModeCapabilities) -> bool:
        from app.execution.firm_execution import paper_auto_submit_allowed

        if not self.settings.effective_auto_execute_hard_stops():
            return False
        if not caps.can_submit:
            return False
        return paper_auto_submit_allowed(self.settings)

    async def _submit_hard_stop(self, lc: PositionLifecycle) -> int:
        """Paper-submit a market exit for a hard-stop lifecycle."""
        from app.execution.order_manager import OrderManager
        from app.execution.validation import ExecutionValidationResult, ValidatedOrderIntent

        if not self.controls.is_new_order_allowed():
            return 0
        qty = abs(float(lc.quantity or 0))
        if qty <= 0:
            return 0
        side = "sell" if float(lc.quantity or 0) >= 0 else "buy"
        key = f"hard-stop:{lc.id}:{datetime.now(UTC).date().isoformat()}"
        validation = ExecutionValidationResult(
            approved=True,
            intents=[
                ValidatedOrderIntent(
                    symbol=lc.symbol.upper(),
                    side=side,
                    quantity=qty,
                    order_type="market",
                    limit_price=None,
                    stop_price=lc.stop_price,
                    idempotency_key=key,
                    decision_id=str(lc.decision_id) if lc.decision_id else str(uuid4()),
                    thesis="hard_stop",
                    venue=getattr(lc, "venue", None)
                    or venue_for_symbol(lc.symbol, self.settings).value,
                    con_id=int(getattr(lc, "con_id", 0) or 0) or None,
                )
            ],
        )
        orders = await OrderManager(self.session, settings=self.settings).submit_validated_intents(
            validation
        )
        return len(orders)

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
            metadata_json={
                "source": "intraday",
                "reason": reason,
                "lifecycle_id": str(lc.id),
                "venue": getattr(lc, "venue", None)
                or venue_for_symbol(lc.symbol, self.settings).value,
                "con_id": int(getattr(lc, "con_id", 0) or 0) or None,
            },
        )
        # Default fail-closed: hard stops wait for approval unless auto-submit is armed.
        if reason == "hard_stop" and not self._should_auto_submit_hard_stops(caps):
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
