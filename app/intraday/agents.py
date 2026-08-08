"""Intraday agent reanalysis orchestration (reuses 6-agent pipeline)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.pipeline import AgentPipeline
from app.core.config import Settings, get_settings
from app.execution.position_manager import PositionManager
from app.execution.safety_controls import TradingControls, trading_controls
from app.intraday.events import IntradayEventBus
from app.intraday.modes import IntradayOperationMode, ModeCapabilities, resolve_mode
from app.models import IntradayAnalysisRun, IntradayDecisionRecord, PositionLifecycle
from app.schemas.cio import CIODecision
from app.schemas.common import PortfolioAction
from app.services.collection import DataCollectionService
from app.services.llm import FakeLLMProvider
from app.universe.service import UniverseService


# Existing-position priority order for symbol actions
_ACTION_PRIORITY = {
    "SELL": 0,
    "PARTIAL_SELL": 1,
    "REDUCE": 2,
    "HEDGE": 3,
    "HOLD": 4,
    "ADD": 5,
    "SCALE_IN": 6,
    "BUY": 7,
    "STRONG_BUY": 8,
}


class IntradayAgentService:
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

    async def evaluate(
        self,
        *,
        fake_llm: bool = True,
        trigger_event_ids: list[str] | None = None,
        parent_decision_id: UUID | None = None,
        bypass_cooldown: bool = False,
    ) -> dict[str, Any]:
        emergency = self.controls.snapshot().state.value == "emergency_stop"
        paused = self.controls.snapshot().state.value == "paused"
        mode = resolve_mode(self.settings, emergency=emergency, paused=paused)
        caps = ModeCapabilities(mode)
        if not caps.can_analyze:
            return {"skipped": True, "reason": f"mode:{mode.value}", "broker_orders_submitted": False}

        if not self.settings.enable_intraday_agent_reanalysis:
            return {"skipped": True, "reason": "enable_intraday_agent_reanalysis_false"}

        open_rows = list(
            (
                await self.session.execute(
                    select(PositionLifecycle).where(PositionLifecycle.status.in_(["OPEN", "ADDING", "REDUCING"]))
                )
            )
            .scalars()
            .all()
        )
        open_syms = [p.symbol for p in open_rows]
        univ = UniverseService(self.session, settings=self.settings)
        horizons: dict[str, str] = {}
        try:
            horizons = await univ.horizon_by_symbol()
        except Exception:  # noqa: BLE001
            horizons = {}
        ok, why = self.bus.allow_reanalysis(
            symbols=open_syms or ["PORTFOLIO"],
            bypass=bypass_cooldown,
            horizon_by_symbol=horizons,
        )
        if not ok:
            return {"skipped": True, "reason": why, "broker_orders_submitted": False}

        run = IntradayAnalysisRun(
            id=uuid4(),
            status="RUNNING",
            trigger_event_ids=trigger_event_ids or [],
            mode=mode.value,
            payload={},
        )
        self.session.add(run)
        await self.session.flush()

        try:
            pm = PositionManager(self.session, settings=self.settings)
            # Prefer recent snapshot from scheduled recon; only re-sync when stale.
            port = await pm.portfolio_state_input()
            snap_age_s: float | None = None
            if port.as_of is not None:
                as_of = port.as_of if port.as_of.tzinfo else port.as_of.replace(tzinfo=UTC)
                snap_age_s = (datetime.now(UTC) - as_of).total_seconds()
            max_age = max(30, int(self.settings.broker_reconciliation_interval_seconds) * 1.5)
            if snap_age_s is None or snap_age_s > max_age:
                try:
                    await pm.sync_from_broker()
                    port = await pm.portfolio_state_input()
                except Exception:  # noqa: BLE001
                    pass
            portfolio = port
            held = [
                p.symbol.upper()
                for p in portfolio.positions
                if abs(p.quantity or 0) > 1e-12
            ] or [s.upper() for s in open_syms]

            entry_universe = await univ.entry_universe()
            horizons = await univ.horizon_by_symbol()
            universe = await univ.collection_universe(holdings=held)

            collection = await DataCollectionService(self.session, persist=False).collect_premarket(
                symbols=universe,
                workflow_id=run.id,
                horizon_by_symbol=horizons,
            )
            llm = FakeLLMProvider({}) if fake_llm else None
            pipeline = (
                AgentPipeline(settings=self.settings, llm=llm)
                if llm
                else AgentPipeline(settings=self.settings)
            )
            analysis = await pipeline.run_from_collection(
                collection,
                portfolio=portfolio,
                proposed_trades=[],
                workflow_id=run.id,
                entry_universe=sorted(entry_universe),
                watchlist_context=[
                    {"symbol": s, "horizon": horizons.get(s, "short")}
                    for s in sorted(entry_universe)
                ],
            )
            from app.services.audit import AuditService

            await AuditService(self.session).persist_analysis(analysis)
            cio: CIODecision = analysis.cio

            # Prioritize existing positions: sort symbol_actions
            held_set = set(held)
            actions = list(cio.symbol_actions)
            actions.sort(
                key=lambda a: (
                    0 if a.symbol.upper() in held_set else 1,
                    _ACTION_PRIORITY.get(a.action.value, 99),
                )
            )

            thesis_status = "INTACT"
            if cio.portfolio_action == PortfolioAction.NO_TRADE:
                thesis_status = "UNKNOWN"
            # Prefer risk veto → no new risk
            portfolio_action = cio.portfolio_action.value
            if not cio.risk_approval:
                portfolio_action = "NO_NEW_RISK"

            # Block new risk in closing / observe modes
            symbol_payload = []
            for a in actions:
                act = a.action.value
                if a.symbol.upper() not in held_set and act in {"BUY", "STRONG_BUY", "SCALE_IN", "ADD"}:
                    if mode == IntradayOperationMode.OBSERVE_ONLY or not caps.can_create_intent:
                        act = "NO_ACTION"
                    elif not self.settings.allow_new_positions_in_closing_window and mode != IntradayOperationMode.PAPER_AUTOMATED:
                        # still allow drafts in manual/analyze; mark later
                        pass
                symbol_payload.append(
                    {
                        "symbol": a.symbol.upper(),
                        "action": act,
                        "confidence": a.confidence,
                        "target_position_pct": a.target_position_pct,
                        "thesis": a.thesis,
                        "stop_loss": a.stop_loss,
                        "thesis_status": thesis_status,
                        "is_existing_position": a.symbol.upper() in held_set,
                        "time_horizon": a.time_horizon.value if a.time_horizon else None,
                    }
                )

            decision_row = IntradayDecisionRecord(
                id=uuid4(),
                parent_decision_id=parent_decision_id or analysis.cio.decision_id,
                analysis_run_id=run.id,
                trigger_event_ids=trigger_event_ids or [],
                as_of=datetime.now(UTC),
                market_regime=cio.market_regime.value,
                thesis_status=thesis_status,
                portfolio_action=portfolio_action,
                symbol_actions=symbol_payload,
                risk_approval=cio.risk_approval,
                risk_conditions=list(cio.risk_conditions or []),
                dissenting_views=[],
                decision_expiry=None,
                payload={"workflow_id": str(run.id), "mode": mode.value},
            )
            self.session.add(decision_row)

            execution: dict[str, Any] = {
                "intent_count": 0,
                "broker_orders_submitted": False,
                "notes": ["intraday_intents_skipped_mode"],
            }
            # Agent firm: materialize CIO → intents when mode allows (not OBSERVE_ONLY draft-only)
            if caps.can_create_intent and not caps.intents_are_draft_only and cio.risk_approval:
                from app.execution.firm_execution import materialize_cio_decision

                prices = {m.symbol: m.last for m in collection.markets}
                execution = await materialize_cio_decision(
                    self.session,
                    cio,
                    portfolio=portfolio,
                    latest_prices=prices,
                    data_quality_score=float(collection.aggregate_quality or 1.0),
                    workflow_id=run.id,
                    settings=self.settings,
                    create_intents=True,
                    allow_submit=caps.can_submit and mode == IntradayOperationMode.PAPER_AUTOMATED,
                    entry_universe=entry_universe,
                    horizon_by_symbol=horizons,
                )
            elif caps.can_create_intent and caps.intents_are_draft_only:
                execution = {"intent_count": 0, "broker_orders_submitted": False, "notes": ["draft_only_mode"]}

            run.status = "COMPLETED"
            run.payload = {
                "decision_id": str(decision_row.id),
                "cio_action": portfolio_action,
                "execution": execution,
                "trading_actor": "cio_bottom_up",
                "universe_size": len(universe),
                "entry_universe_size": len(entry_universe),
            }
            self.bus.record_reanalysis(open_syms or ["PORTFOLIO"])
            await self.session.flush()
            return {
                "analysis_run_id": str(run.id),
                "intraday_decision_id": str(decision_row.id),
                "portfolio_action": portfolio_action,
                "thesis_status": thesis_status,
                "symbol_actions": symbol_payload,
                "mode": mode.value,
                "intent_drafts_allowed": caps.can_create_intent,
                "intent_count": execution.get("intent_count", 0),
                "broker_orders_submitted": bool(execution.get("broker_orders_submitted")),
                "trading_actor": "cio_bottom_up",
                "execution": execution,
            }
        except Exception as exc:  # noqa: BLE001
            run.status = "FAILED"
            run.payload = {"error": str(exc)[:300]}
            await self.session.flush()
            # Analysis failure must NOT cancel protection orders
            return {
                "analysis_run_id": str(run.id),
                "failed": True,
                "error": str(exc)[:300],
                "protection_orders_cancelled": False,
                "broker_orders_submitted": False,
            }
