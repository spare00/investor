"""Workflow orchestration: premarket, intraday, postmarket (no broker submit yet)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.pipeline import AgentPipeline, AnalysisBundle
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.metrics import (
    HARD_VETOES,
    OPEN_POSITIONS,
    ORDERS_BLOCKED,
    ORDERS_SUBMITTED,
    PORTFOLIO_CASH,
    PORTFOLIO_DRAWDOWN_PCT,
    PORTFOLIO_EQUITY,
    TRADING_STATE,
    WORKFLOW_DURATION,
    WORKFLOW_RUNS,
    trading_state_value,
)
from app.execution.order_manager import OrderManager
from app.execution.position_manager import PositionManager
from app.execution.safety_controls import trading_controls
from app.execution.validation import ExecutionValidationResult, ExecutionValidator
from app.models import Order
from app.risk import PortfolioRiskView, PositionRiskView
from app.schemas.risk_manager import PortfolioStateInput, ProposedTrade
from app.services.audit import AuditService
from app.services.collection import CollectionBundle, DataCollectionService
from app.services.llm import LLMClient, get_llm_client
from app.services.market_hours import is_regular_session, minutes_to_close
from app.storage.repositories import SystemEventRepository

logger = get_logger(__name__)


@dataclass(slots=True)
class WorkflowResult:
    workflow_id: UUID
    kind: str
    started_at: datetime
    finished_at: datetime
    collection: CollectionBundle | None = None
    analysis: AnalysisBundle | None = None
    validation: ExecutionValidationResult | None = None
    orders: list[Order] = field(default_factory=list)
    skipped_reason: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": str(self.workflow_id),
            "kind": self.kind,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "skipped_reason": self.skipped_reason,
            "notes": self.notes,
            "collection": None
            if self.collection is None
            else {
                "aggregate_quality": self.collection.aggregate_quality,
                "fail_closed": self.collection.fail_closed,
                "news": len(self.collection.news),
                "markets": len(self.collection.markets),
            },
            "cio": None
            if self.analysis is None
            else self.analysis.cio.model_dump(mode="json"),
            "risk": None
            if self.analysis is None
            else self.analysis.risk.model_dump(mode="json"),
            "validation": None
            if self.validation is None
            else {
                "approved": self.validation.approved,
                "rejections": self.validation.rejections,
                "intent_count": len(self.validation.intents),
                "intents": [
                    {
                        "symbol": i.symbol,
                        "side": i.side,
                        "quantity": i.quantity,
                        "idempotency_key": i.idempotency_key,
                    }
                    for i in self.validation.intents
                ],
            },
            "orders": [
                {
                    "id": str(o.id),
                    "symbol": o.symbol,
                    "side": o.side,
                    "qty": o.qty,
                    "status": o.status,
                    "broker_order_id": o.broker_order_id,
                    "idempotency_key": o.idempotency_key,
                }
                for o in self.orders
            ],
        }


class WorkflowService:
    """Bottom-up workflows ending at validated order intents (Phase 6 submits)."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        llm: LLMClient | None = None,
        persist: bool = True,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.llm = llm or get_llm_client(self.settings)
        self.persist = persist
        self.events = SystemEventRepository(session)
        self._last_intraday_at: datetime | None = None

    def _default_portfolio(self, as_of: datetime) -> PortfolioStateInput:
        return PortfolioStateInput(
            as_of=as_of,
            equity=self.settings.starting_cash,
            cash=self.settings.starting_cash,
            cash_pct=100.0,
            gross_exposure_pct=0.0,
        )

    def _portfolio_risk_view(self, portfolio: PortfolioStateInput) -> PortfolioRiskView:
        return PortfolioRiskView(
            equity=portfolio.equity,
            cash=portfolio.cash,
            cash_pct=portfolio.cash_pct,
            gross_exposure_pct=portfolio.gross_exposure_pct,
            positions=[
                PositionRiskView(
                    symbol=p.symbol,
                    quantity=p.quantity,
                    market_value=p.market_value,
                    sector=p.sector,
                    weight_pct=p.weight_pct,
                )
                for p in portfolio.positions
            ],
            daily_pnl_pct=portfolio.daily_pnl_pct,
            drawdown_pct=portfolio.drawdown_pct,
            consecutive_losses=portfolio.consecutive_losses,
            trading_halted=portfolio.trading_halted,
            cooldown_until=portfolio.cooldown_until,
        )

    async def _link_briefing_to_daily(
        self,
        *,
        workflow_id: UUID,
        decision_id: UUID | None,
        kind: str,
        now: datetime,
        cio_action: str | None = None,
        risk_verdict: str | None = None,
    ) -> None:
        """Stamp today's DailyWorkflowRun so Briefing can find WorkflowService dumps."""
        try:
            from app.market.calendar import MarketCalendarService
            from app.workflow.daily import DailyWorkflowService

            status = MarketCalendarService(self.settings).get_market_status(now)
            session_date = status.session_date.isoformat()
            daily = await DailyWorkflowService(self.session, settings=self.settings).get_current(
                session_date
            )
            if daily is None:
                return
            meta = dict(daily.metadata_json or {})
            meta["last_briefing_workflow_id"] = str(workflow_id)
            meta["last_briefing_kind"] = kind
            meta["last_briefing_at"] = now.isoformat()
            if cio_action:
                meta["cio_action"] = cio_action
            if risk_verdict:
                meta["risk_verdict"] = risk_verdict
            daily.metadata_json = meta
            if decision_id is not None:
                daily.latest_decision_id = decision_id
            await self.session.flush()
        except Exception as exc:  # noqa: BLE001
            logger.warning("briefing_daily_link_failed", error=str(exc)[:200])

    def _block_new_entries_now(self, as_of: datetime | None = None) -> bool:
        """True in closing / force-close window when new entries are disabled."""
        if self.settings.allow_new_positions_in_closing_window:
            return False
        try:
            from app.market.calendar import MarketCalendarService

            status = MarketCalendarService(self.settings).get_market_status(as_of)
            return bool(status.in_closing_window or status.in_force_close_window)
        except Exception:  # noqa: BLE001
            mtc = minutes_to_close(as_of or datetime.now(UTC))
            if mtc is None:
                return False
            return mtc <= max(
                self.settings.closing_window_minutes_before_close,
                self.settings.force_close_before_market_close_minutes,
            )

    async def run_premarket(
        self,
        *,
        portfolio: PortfolioStateInput | None = None,
        proposed_trades: list[ProposedTrade] | None = None,
        workflow_id: UUID | None = None,
    ) -> WorkflowResult:
        started = datetime.now(UTC)
        wf = workflow_id or uuid4()
        notes: list[str] = []

        if not trading_controls.is_new_order_allowed():
            snap = trading_controls.snapshot()
            msg = f"Trading controls block workflow: {snap.state.value}"
            notes.append(msg)
            if self.persist:
                await self.events.record(
                    level="warning",
                    event_type="workflow_premarket_blocked",
                    message=msg,
                    workflow_id=wf,
                )
            # Still allow analysis for observability, but validation will fail closed.

        # Sync portfolio first so collection covers allowlist ∪ existing holdings.
        port = portfolio
        if port is None:
            try:
                pm = PositionManager(self.session, settings=self.settings)
                await pm.sync_from_broker()
                port = await pm.portfolio_state_input()
                notes.append("portfolio_from_broker")
            except Exception:  # noqa: BLE001
                port = self._default_portfolio(datetime.now(UTC))
                notes.append("portfolio_default_fallback")

        held = [p.symbol for p in port.positions]
        from app.universe.context import load_last_regime_context
        from app.universe.service import UniverseService

        univ = UniverseService(self.session, settings=self.settings)
        await univ.ensure_seeded()
        ctx = await load_last_regime_context(self.session)
        # Refresh focus lightly without mandatory LLM on every premarket if disabled;
        # when enabled, run manager once per premarket with prior regime/themes.
        if self.settings.universe_manager_enabled and univ.is_dynamic():
            try:
                refreshed = await univ.refresh(
                    holdings=held,
                    market_regime=ctx.get("market_regime"),
                    themes=list(ctx.get("themes") or []),
                )
                if refreshed.get("skipped") and refreshed.get("reason") == "min_interval":
                    notes.append("universe_refresh_deferred_weekly")
                else:
                    notes.append("universe_refreshed")
                try:
                    from app.workflow.daily import DailyWorkflowService

                    replan = await DailyWorkflowService(
                        self.session, settings=self.settings
                    ).replan_intraday_jobs()
                    if not replan.get("skipped"):
                        notes.append(
                            f"intraday_replan:{replan.get('purged')}/{replan.get('created')}"
                        )
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"intraday_replan_failed:{exc}")
                if ctx.get("market_regime") or ctx.get("themes"):
                    notes.append(
                        f"universe_context_prior:{ctx.get('market_regime') or 'n/a'}:"
                        f"{','.join((ctx.get('themes') or [])[:3]) or 'none'}"
                    )
            except Exception as exc:  # noqa: BLE001
                await univ.build_focus_without_llm(holdings=held)
                notes.append(f"universe_refresh_fallback:{exc}")
        else:
            await univ.build_focus_without_llm(holdings=held)

        universe = await univ.collection_universe(holdings=held)
        entry_universe = await univ.entry_universe()
        horizons = await univ.horizon_by_symbol()

        collection = await DataCollectionService(
            self.session, settings=self.settings, persist=self.persist
        ).collect_premarket(symbols=universe, workflow_id=wf, horizon_by_symbol=horizons)

        if collection.fail_closed:
            notes.append("collection_fail_closed")

        analysis = await AgentPipeline(settings=self.settings, llm=self.llm).run_from_collection(
            collection,
            portfolio=port,
            proposed_trades=proposed_trades or [],
            workflow_id=wf,
            entry_universe=sorted(entry_universe),
            watchlist_context=[
                {"symbol": s, "horizon": horizons.get(s, "short")} for s in sorted(entry_universe)
            ],
        )

        # After agents: boost theme-aligned names + rebuild focus for rest of session / next cycle.
        try:
            regime = analysis.macro.market_regime.value
            themes = list(analysis.market_intelligence.top_market_themes or [])
            applied = await univ.apply_session_context(
                holdings=held, market_regime=regime, themes=themes
            )
            notes.append(
                f"universe_context_applied:{regime}:boosted={applied.get('boosted', 0)}"
            )
            # Refresh entry/horizon map after priority boosts (same symbols, updated focus).
            entry_universe = await univ.entry_universe()
            horizons = await univ.horizon_by_symbol()
        except Exception as exc:  # noqa: BLE001
            notes.append(f"universe_context_apply_failed:{exc}")

        prices = {m.symbol: m.last for m in collection.markets}
        seen_keys = await OrderManager(self.session, settings=self.settings).seen_idempotency_keys()
        block_entries = self._block_new_entries_now(started)
        if block_entries:
            notes.append("closing_window_block_new_entries")
        validation = ExecutionValidator(settings=self.settings).validate(
            analysis.cio,
            portfolio=self._portfolio_risk_view(port),
            latest_prices=prices,
            data_quality_score=collection.aggregate_quality,
            market_session_clear=not collection.fail_closed,
            broker_data_consistent=True,
            workflow_id=str(wf),
            seen_idempotency_keys=seen_keys,
            entry_universe=entry_universe,
            horizon_by_symbol=horizons,
            block_new_entries=block_entries,
        )

        orders: list[Order] = []
        from app.execution.firm_execution import materialize_cio_decision

        execution = await materialize_cio_decision(
            self.session,
            analysis.cio,
            portfolio=self._portfolio_risk_view(port),
            latest_prices=prices,
            data_quality_score=collection.aggregate_quality,
            workflow_id=wf,
            settings=self.settings,
            create_intents=not collection.fail_closed,
            allow_submit=not collection.fail_closed,
            entry_universe=entry_universe,
            horizon_by_symbol=horizons,
            block_new_entries=block_entries,
            market_session_clear=not collection.fail_closed,
        )
        notes.extend(execution.get("notes") or [])
        # Compatibility: WorkflowResult.orders length reflects submitted count
        if execution.get("orders_submitted", 0) > 0:
            from sqlalchemy import select as sa_select

            from app.models import Order as OrderModel

            orders = list(
                (
                    await self.session.execute(
                        sa_select(OrderModel)
                        .where(OrderModel.decision_id == analysis.cio.decision_id)
                        .limit(int(execution["orders_submitted"]))
                    )
                )
                .scalars()
                .all()
            )

        if self.persist:
            try:
                await AuditService(self.session).persist_analysis(analysis)
                notes.append("audit_persisted")
            except Exception as exc:  # noqa: BLE001
                notes.append(f"audit_persist_failed:{exc}")
                logger.exception("audit_persist_failed", workflow_id=str(wf))

        for code in analysis.risk.hard_vetoes:
            HARD_VETOES.labels(code=str(code)[:64]).inc()
        for order in orders:
            ORDERS_SUBMITTED.labels(
                symbol=order.symbol, side=order.side, status=order.status
            ).inc()

        TRADING_STATE.set(trading_state_value(trading_controls.snapshot().state.value))
        finished = datetime.now(UTC)
        await self._link_briefing_to_daily(
            workflow_id=wf,
            decision_id=analysis.cio.decision_id,
            kind="premarket",
            now=finished,
            cio_action=analysis.cio.portfolio_action.value,
            risk_verdict=analysis.risk.overall_verdict.value,
        )
        WORKFLOW_DURATION.labels(kind="premarket").observe(
            (finished - started).total_seconds()
        )
        outcome = "ok"
        if collection.fail_closed:
            outcome = "fail_closed"
        elif not validation.approved and validation.intents:
            outcome = "validation_rejected"
        elif orders:
            outcome = "orders_submitted"
        WORKFLOW_RUNS.labels(kind="premarket", outcome=outcome).inc()

        logger.info(
            "workflow_premarket_done",
            workflow_id=str(wf),
            fail_closed=collection.fail_closed,
            cio=analysis.cio.portfolio_action.value,
            validated=validation.approved,
            orders=len(orders),
        )
        return WorkflowResult(
            workflow_id=wf,
            kind="premarket",
            started_at=started,
            finished_at=finished,
            collection=collection,
            analysis=analysis,
            validation=validation,
            orders=orders,
            notes=notes,
        )

    async def run_intraday_evaluate(
        self,
        *,
        portfolio: PortfolioStateInput | None = None,
        force: bool = False,
        now: datetime | None = None,
    ) -> WorkflowResult:
        started = now or datetime.now(UTC)
        wf = uuid4()
        notes: list[str] = []

        # Horizon-aware cooldown: scalp books can re-eval faster than medium.
        held_syms: list[str] = []
        if portfolio is not None:
            held_syms = [p.symbol for p in portfolio.positions if p.quantity]
        else:
            try:
                pm = PositionManager(self.session, settings=self.settings)
                port_probe = await pm.portfolio_state_input()
                held_syms = [p.symbol for p in port_probe.positions if p.quantity]
            except Exception:  # noqa: BLE001
                held_syms = []
        try:
            from app.universe.reeval import min_reeval_seconds_for_symbols
            from app.universe.service import UniverseService

            horizons = await UniverseService(self.session, settings=self.settings).horizon_by_symbol()
            min_secs = min_reeval_seconds_for_symbols(held_syms, horizons, self.settings)
        except Exception:  # noqa: BLE001
            min_secs = self.settings.intraday_min_reeval_seconds
        min_interval = timedelta(seconds=min_secs)
        notes.append(f"reeval_interval_seconds={min_secs}")
        if (
            not force
            and self._last_intraday_at is not None
            and started - self._last_intraday_at < min_interval
        ):
            return WorkflowResult(
                workflow_id=wf,
                kind="intraday",
                started_at=started,
                finished_at=datetime.now(UTC),
                skipped_reason="min_reeval_interval",
                notes=["cooldown_active", f"reeval_interval_seconds={min_secs}"],
            )

        if not force and not is_regular_session(started):
            return WorkflowResult(
                workflow_id=wf,
                kind="intraday",
                started_at=started,
                finished_at=datetime.now(UTC),
                skipped_reason="outside_regular_session",
            )

        mtc = minutes_to_close(started)
        if mtc is not None and mtc <= self.settings.force_close_before_market_close_minutes:
            notes.append(f"force_close_window_minutes={mtc}")

        # Reuse premarket pipeline on fresh data for intraday re-eval.
        result = await self.run_premarket(portfolio=portfolio, workflow_id=wf)
        result.kind = "intraday"
        result.notes = notes + result.notes
        if result.analysis is not None:
            await self._link_briefing_to_daily(
                workflow_id=result.workflow_id,
                decision_id=result.analysis.cio.decision_id,
                kind="intraday_manual",
                now=datetime.now(UTC),
                cio_action=result.analysis.cio.portfolio_action.value,
                risk_verdict=result.analysis.risk.overall_verdict.value,
            )
        if mtc is not None and mtc <= self.settings.force_close_before_market_close_minutes:
            try:
                from app.intraday.closing import ClosingService

                closing = await ClosingService(self.session, settings=self.settings).run_closing(
                    in_closing_window=True
                )
                closes = [p for p in closing.get("plans", []) if p.get("action") == "close"]
                result.notes.append(f"force_close_plans={len(closes)}")
                if closing.get("intent_ids"):
                    result.notes.append(f"force_close_intents={len(closing['intent_ids'])}")
                if closing.get("orders_submitted"):
                    result.notes.append(f"force_close_orders={closing['orders_submitted']}")
                result.notes.extend(closing.get("notes") or [])
            except Exception as exc:  # noqa: BLE001
                result.notes.append(f"force_close_failed:{exc}")
        self._last_intraday_at = started
        return result

    async def run_postmarket(
        self,
        *,
        portfolio: PortfolioStateInput | None = None,
    ) -> WorkflowResult:
        started = datetime.now(UTC)
        wf = uuid4()
        notes = ["postmarket_review"]

        collection = await DataCollectionService(
            self.session, settings=self.settings, persist=self.persist
        ).collect_premarket(workflow_id=wf)

        port = portfolio or self._default_portfolio(started)
        analysis = await AgentPipeline(settings=self.settings, llm=self.llm).run_from_collection(
            collection,
            portfolio=port,
            proposed_trades=[],
            workflow_id=wf,
        )

        # Postmarket: do not produce new entry intents — validation with empty actions.
        from app.schemas.cio import CIODecision
        from app.schemas.common import PortfolioAction

        review_decision = analysis.cio.model_copy(
            update={
                "portfolio_action": PortfolioAction.HOLD,
                "symbol_actions": [],
                "reason_not_to_trade": "postmarket_review_only",
            }
        )
        # Ensure schema-safe if risk_approval false already.
        if not review_decision.risk_approval and review_decision.portfolio_action in {
            PortfolioAction.BUY,
            PortfolioAction.STRONG_BUY,
            PortfolioAction.SCALE_IN,
        }:
            review_decision = CIODecision(
                timestamp=analysis.cio.timestamp,
                market_regime=analysis.cio.market_regime,
                portfolio_action=PortfolioAction.HOLD,
                symbol_actions=[],
                cash_target_pct=analysis.cio.cash_target_pct,
                risk_approval=False,
                reason_not_to_trade="postmarket_review_only",
            )

        validation = ExecutionValidator(settings=self.settings).validate(
            review_decision,
            portfolio=self._portfolio_risk_view(port),
            latest_prices={m.symbol: m.last for m in collection.markets},
            data_quality_score=collection.aggregate_quality,
            workflow_id=str(wf),
        )
        notes.append(
            f"decision_quality_proxy={analysis.devil.challenge_score:.2f}",
        )
        if self.persist:
            await self.events.record(
                level="info",
                event_type="workflow_postmarket_complete",
                message="Post-market review completed",
                context={
                    "regime": analysis.macro.market_regime.value,
                    "cio_action": analysis.cio.portfolio_action.value,
                },
                workflow_id=wf,
            )

        return WorkflowResult(
            workflow_id=wf,
            kind="postmarket",
            started_at=started,
            finished_at=datetime.now(UTC),
            collection=collection,
            analysis=analysis,
            validation=validation,
            notes=notes,
        )
