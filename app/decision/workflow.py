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
from app.execution.order_manager import OrderManager
from app.execution.position_manager import PositionManager
from app.execution.safety_controls import trading_controls
from app.execution.validation import ExecutionValidationResult, ExecutionValidator
from app.models import Order
from app.risk import PortfolioRiskView, PositionRiskView
from app.schemas.risk_manager import PortfolioStateInput, ProposedTrade
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

        collection = await DataCollectionService(
            self.session, settings=self.settings, persist=self.persist
        ).collect_premarket(workflow_id=wf)

        if collection.fail_closed:
            notes.append("collection_fail_closed")

        # Prefer live paper account state when broker is reachable.
        port = portfolio
        if port is None:
            try:
                pm = PositionManager(self.session, settings=self.settings)
                await pm.sync_from_broker()
                port = await pm.portfolio_state_input()
                notes.append("portfolio_from_broker")
            except Exception:  # noqa: BLE001
                port = self._default_portfolio(collection.collected_at)
                notes.append("portfolio_default_fallback")
        analysis = await AgentPipeline(settings=self.settings, llm=self.llm).run_from_collection(
            collection,
            portfolio=port,
            proposed_trades=proposed_trades or [],
            workflow_id=wf,
        )

        prices = {m.symbol: m.last for m in collection.markets}
        seen_keys = await OrderManager(self.session, settings=self.settings).seen_idempotency_keys()
        validation = ExecutionValidator(settings=self.settings).validate(
            analysis.cio,
            portfolio=self._portfolio_risk_view(port),
            latest_prices=prices,
            data_quality_score=collection.aggregate_quality,
            market_session_clear=not collection.fail_closed,
            broker_data_consistent=True,
            workflow_id=str(wf),
            seen_idempotency_keys=seen_keys,
        )

        orders: list[Order] = []
        if validation.approved and validation.intents:
            if self.settings.trading_mode.value == "paper" or (
                self.settings.trading_mode.value == "live"
                and self.settings.is_live_trading_allowed()
            ):
                try:
                    orders = await OrderManager(
                        self.session, settings=self.settings
                    ).submit_validated_intents(
                        validation,
                        decision_id=analysis.cio.decision_id,
                        workflow_id=wf,
                    )
                    notes.append(f"orders_submitted={len(orders)}")
                    # Sync positions after fills (paper often fills immediately)
                    try:
                        await PositionManager(self.session, settings=self.settings).sync_from_broker()
                        notes.append("positions_synced")
                    except Exception as exc:  # noqa: BLE001
                        notes.append(f"position_sync_failed:{exc}")
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"order_submit_failed:{exc}")
                    logger.exception("workflow_order_submit_failed", workflow_id=str(wf))
            else:
                notes.append("submit_skipped_mode")
        elif validation.rejections:
            notes.append(f"validation_rejected:{','.join(validation.rejections[:5])}")

        finished = datetime.now(UTC)
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

        min_interval = timedelta(seconds=self.settings.intraday_min_reeval_seconds)
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
                notes=["cooldown_active"],
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
