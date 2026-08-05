"""Daily workflow orchestration — 6-agent firm day cycle.

Trading authority: CIO bottom-up decision → Order Intents → paper broker when
safety flags unlock. Live trading stays blocked. Manual approval is an optional
ops brake, not the firm identity.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.pipeline import AgentPipeline
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.execution.safety_controls import TradingControls, trading_controls
from app.market.calendar import MarketCalendarService
from app.models import DailyWorkflowRun, ScheduledJobRecord, WorkflowStateTransition
from app.schemas.risk_manager import PortfolioStateInput
from app.ingestion.pipeline import DataCollectionPipeline
from app.services.collection import DataCollectionService
from app.services.llm import FakeLLMProvider, get_llm_client
from app.workflow.closing import ClosingPolicyEngine
from app.workflow.lease import LeaseError, LeaseService
from app.workflow.revalidation import RevalidationService
from app.workflow.states import (
    BROKER_ORDERS_ALLOWED,
    ClosingPolicy,
    DailyWorkflowState,
    IntradayEvalResult,
    RevalidationResult,
    WorkflowRunStatus,
    assert_transition_allowed,
)

logger = get_logger(__name__)


class DailyWorkflowError(Exception):
    pass


class DailyWorkflowService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        controls: TradingControls | None = None,
        owner: str = "daily-workflow",
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.controls = controls or trading_controls
        self.calendar = MarketCalendarService(self.settings)
        self.leases = LeaseService(session, self.settings)
        self.revalidation = RevalidationService(session, settings=self.settings, calendar=self.calendar)
        self.closing = ClosingPolicyEngine()
        self.owner = owner

    def _broker_guard(self) -> None:
        """Warn if Live is misconfigured; never silently ignore paper automation flags."""
        if self.settings.enable_live_trading or self.settings.is_live_trading_allowed():
            logger.error(
                "live_trading_flag_set_daily_workflow_will_not_submit_live",
                enable_live_trading=self.settings.enable_live_trading,
            )
        if self.settings.enable_broker_orders and self.settings.enable_automated_execution:
            logger.info(
                "daily_workflow_agent_paper_path_armed",
                require_manual_order_approval=self.settings.require_manual_order_approval,
            )

    async def get_current(self, session_date: str | None = None) -> DailyWorkflowRun | None:
        if session_date is None:
            session_date = datetime.now(self.calendar.market_tz).date().isoformat()
        return (
            await self.session.execute(
                select(DailyWorkflowRun).where(
                    DailyWorkflowRun.session_date == session_date,
                    DailyWorkflowRun.calendar_name == self.settings.market_calendar,
                )
            )
        ).scalar_one_or_none()

    async def prepare(self, *, session_date: str | None = None, now: datetime | None = None) -> dict[str, Any]:
        self._broker_guard()
        now = now or datetime.now(UTC)
        day = (
            datetime.fromisoformat(session_date).date()
            if session_date
            else now.astimezone(self.calendar.market_tz).date()
        )
        lease_key = f"daily:{self.settings.market_calendar}:{day.isoformat()}:prepare"
        await self.leases.acquire(lease_key, self.owner)
        try:
            if self.controls.snapshot().state.value == "emergency_stop":
                raise DailyWorkflowError("emergency_stop_active")

            existing = await self.get_current(day.isoformat())
            if existing is not None:
                return self._run_dict(existing, note="already_prepared")

            session = self.calendar.get_session(day)
            if not session.is_trading_day:
                run = DailyWorkflowRun(
                    id=uuid4(),
                    session_date=day.isoformat(),
                    calendar_name=self.settings.market_calendar,
                    current_state=DailyWorkflowState.NON_TRADING_DAY.value,
                    status=WorkflowRunStatus.COMPLETED.value,
                    started_at=now,
                    completed_at=now,
                    timezone=str(self.calendar.market_tz),
                    early_close=False,
                    metadata_json={"note": "non_trading_day"},
                )
                self.session.add(run)
                await self.session.flush()
                await self._transition(
                    run,
                    DailyWorkflowState.NON_TRADING_DAY,
                    DailyWorkflowState.COMPLETED,
                    trigger="prepare",
                    reason="weekend_or_holiday",
                )
                # Stay on NON_TRADING then mark completed via direct set for audit simplicity
                run.current_state = DailyWorkflowState.COMPLETED.value
                await self._plan_jobs(run, session)
                return self._run_dict(run)

            run = DailyWorkflowRun(
                id=uuid4(),
                session_date=day.isoformat(),
                calendar_name=self.settings.market_calendar,
                current_state=DailyWorkflowState.PREMARKET_PREPARATION.value,
                status=WorkflowRunStatus.RUNNING.value,
                started_at=now,
                timezone=str(self.calendar.market_tz),
                market_open_at=session.regular_open,
                market_close_at=session.regular_close,
                early_close=session.is_early_close,
                metadata_json={"session": session.to_dict()},
            )
            self.session.add(run)
            await self.session.flush()
            await self._plan_jobs(run, session)
            return self._run_dict(run)
        finally:
            try:
                await self.leases.release(lease_key, self.owner)
            except LeaseError:
                pass

    async def run_analysis(
        self,
        *,
        session_date: str | None = None,
        fake_llm: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        self._broker_guard()
        now = now or datetime.now(UTC)
        run = await self._require_run(session_date)
        self._assert_not_blocked(run)
        lease_key = f"daily:{run.session_date}:analysis"
        await self.leases.acquire(lease_key, self.owner)
        try:
            if run.current_state == DailyWorkflowState.PREMARKET_PREPARATION.value:
                await self._set_state(
                    run,
                    DailyWorkflowState.PREMARKET_ANALYSIS,
                    trigger="run_analysis",
                    reason="start_analysis",
                )
            elif run.current_state not in {
                DailyWorkflowState.PREMARKET_ANALYSIS.value,
                DailyWorkflowState.PREOPEN_REVALIDATION.value,
            }:
                raise DailyWorkflowError(f"analysis_not_allowed_from:{run.current_state}")

            llm = FakeLLMProvider({}) if fake_llm else get_llm_client(self.settings)
            data = await DataCollectionPipeline(
                self.session, settings=self.settings, fixture_mode=True
            ).collect("PREMARKET", workflow_id=run.id)
            collection = data.legacy_bundle
            if collection is None:
                collection = await DataCollectionService(
                    self.session, settings=self.settings, persist=True
                ).collect_premarket(workflow_id=run.id)
            if data.fail_closed:
                meta = dict(run.metadata_json or {})
                meta["data_fail_closed"] = True
                meta["data_fail_closed_reasons"] = data.fail_closed_reasons
                meta["collection_run_id"] = str(data.collection_run_id)
                run.metadata_json = meta
            portfolio = PortfolioStateInput(
                as_of=now,
                equity=self.settings.starting_cash,
                cash=self.settings.starting_cash,
                cash_pct=100.0,
                gross_exposure_pct=0.0,
            )
            analysis = await AgentPipeline(settings=self.settings, llm=llm).run_from_collection(
                collection,
                portfolio=portfolio,
                proposed_trades=[],
                workflow_id=run.id,
            )
            prices = {m.symbol: m.last for m in collection.markets}
            from app.execution.firm_execution import materialize_cio_decision

            # Agent firm path: CIO decides → intents; paper submit when automation unlocked
            execution = await materialize_cio_decision(
                self.session,
                analysis.cio,
                portfolio=portfolio,
                latest_prices=prices,
                data_quality_score=float(
                    (data.quality_summary or {}).get("aggregate_score")
                    or collection.aggregate_quality
                    or 1.0
                ),
                workflow_id=run.id,
                settings=self.settings,
                create_intents=not (data.fail_closed or collection.fail_closed),
                allow_submit=not (data.fail_closed or collection.fail_closed),
            )
            meta = dict(run.metadata_json or {})
            meta.update(
                {
                    "analysis_completed_at": analysis.completed_at.isoformat(),
                    "cio_action": analysis.cio.portfolio_action.value,
                    "risk_verdict": analysis.risk.overall_verdict.value,
                    "broker_orders_submitted": bool(execution.get("broker_orders_submitted")),
                    "intent_count": execution.get("intent_count", 0),
                    "intent_ids": execution.get("intent_ids", []),
                    "execution_notes": execution.get("notes", []),
                    "trading_actor": "cio_bottom_up",
                    "collection_run_id": str(data.collection_run_id),
                    "data_quality_summary": data.quality_summary,
                    "market_events": data.market_events[:20],
                }
            )
            if data.fail_closed or collection.fail_closed:
                # Force NO_TRADE path visibility without broker
                meta["no_trade_reason"] = ",".join(data.fail_closed_reasons) or "collection_fail_closed"
            run.metadata_json = meta
            run.analysis_workflow_run_id = analysis.workflow_id
            run.latest_decision_id = analysis.cio.decision_id
            await self._set_state(
                run,
                DailyWorkflowState.PREOPEN_REVALIDATION,
                trigger="run_analysis",
                reason="analysis_complete",
            )
            return {
                **self._run_dict(run),
                "analysis": {
                    "workflow_id": str(analysis.workflow_id),
                    "cio_action": analysis.cio.portfolio_action.value,
                    "broker_orders_submitted": bool(execution.get("broker_orders_submitted")),
                    "intent_count": execution.get("intent_count", 0),
                    "trading_actor": "cio_bottom_up",
                },
                "execution": execution,
                "data": data.to_dict(),
            }
        finally:
            try:
                await self.leases.release(lease_key, self.owner)
            except LeaseError:
                pass

    async def revalidate(
        self,
        *,
        session_date: str | None = None,
        fixture: dict[str, Any] | None = None,
        now: datetime | None = None,
        fake_llm: bool = False,
        _reentry_depth: int = 0,
    ) -> dict[str, Any]:
        self._broker_guard()
        now = now or datetime.now(UTC)
        run = await self._require_run(session_date)
        self._assert_not_blocked(run)
        if run.current_state != DailyWorkflowState.PREOPEN_REVALIDATION.value:
            raise DailyWorkflowError(f"revalidate_not_allowed_from:{run.current_state}")
        report = await self.revalidation.revalidate(run, now=now, fixture=fixture)
        if report.result == RevalidationResult.REANALYSIS_REQUIRED:
            if report.attempt > self.settings.max_revalidation_retries or _reentry_depth >= 2:
                # Prefer entering the day in a no-trade posture over leaving the
                # session stuck in PREOPEN_REVALIDATION (no second scheduled job).
                meta = dict(run.metadata_json or {})
                meta["no_trade_reason"] = f"reanalysis_unresolved:{report.reason}"
                run.metadata_json = meta
                await self._set_state(
                    run,
                    DailyWorkflowState.MARKET_OPEN,
                    trigger="revalidate",
                    reason="reanalysis_unresolved_enter",
                )
                await self._set_state(
                    run,
                    DailyWorkflowState.INTRADAY,
                    trigger="revalidate",
                    reason="enter_intraday_after_reanalysis_limit",
                )
                return {
                    **self._run_dict(run),
                    "revalidation": report.to_dict(),
                    "note": "entered_intraday_after_reanalysis_limit",
                }
            await self._set_state(
                run,
                DailyWorkflowState.PREMARKET_ANALYSIS,
                trigger="revalidate",
                reason="reanalysis_required",
            )
            follow_up = await self.run_analysis(
                session_date=run.session_date, fake_llm=fake_llm, now=now
            )
            await self._mark_reanalysis_incorporated(run, now=now)
            # Same job continues: re-check after fresh analysis (do not re-apply
            # one-shot fixtures like stale_data that forced the first reanalysis).
            settled = await self.revalidate(
                session_date=run.session_date,
                fixture=None,
                now=now,
                fake_llm=fake_llm,
                _reentry_depth=_reentry_depth + 1,
            )
            return {
                **settled,
                "follow_up": follow_up,
                "prior_revalidation": report.to_dict(),
            }
        if report.result in {RevalidationResult.VALID, RevalidationResult.VALID_WITH_RESTRICTIONS}:
            await self._set_state(
                run,
                DailyWorkflowState.MARKET_OPEN,
                trigger="revalidate",
                reason=report.result.value,
            )
            await self._set_state(
                run,
                DailyWorkflowState.INTRADAY,
                trigger="revalidate",
                reason="enter_intraday",
            )
        elif report.result == RevalidationResult.NO_TRADE:
            meta = dict(run.metadata_json or {})
            meta["no_trade_reason"] = report.reason
            run.metadata_json = meta
            await self._set_state(
                run,
                DailyWorkflowState.MARKET_OPEN,
                trigger="revalidate",
                reason="no_trade_carry",
            )
            await self._set_state(
                run,
                DailyWorkflowState.INTRADAY,
                trigger="revalidate",
                reason="enter_intraday_no_trade",
            )
        else:
            await self._set_state(
                run,
                DailyWorkflowState.FAILED,
                trigger="revalidate",
                reason=report.reason,
            )
        return {**self._run_dict(run), "revalidation": report.to_dict()}

    async def _mark_reanalysis_incorporated(
        self, run: DailyWorkflowRun, *, now: datetime
    ) -> None:
        meta = dict(run.metadata_json or {})
        events = list(meta.get("market_events") or [])
        changed = False
        for ev in events:
            if not isinstance(ev, dict):
                continue
            if ev.get("requires_reanalysis"):
                ev["requires_reanalysis"] = False
                ev["incorporated_at"] = now.isoformat()
                changed = True
        if changed:
            meta["market_events"] = events
            run.metadata_json = meta
            await self.session.flush()

    def _should_catch_up_session(self, run: DailyWorkflowRun, now: datetime) -> bool:
        """True when prep is incomplete and market is near open or already open."""
        if run.current_state not in {
            DailyWorkflowState.PREMARKET_PREPARATION.value,
            DailyWorkflowState.PREMARKET_ANALYSIS.value,
            DailyWorkflowState.PREOPEN_REVALIDATION.value,
        }:
            return False
        status = self.calendar.get_market_status(now)
        if not status.is_trading_day:
            return False
        if status.phase in {"REGULAR", "CLOSING"} or status.in_closing_window or status.in_force_close_window:
            return True
        mins = status.minutes_to_open
        if mins is None:
            return False
        # Inside the preopen revalidation window (default 10m), or analysis lead
        # when we have not even analyzed yet.
        if mins <= self.settings.preopen_revalidation_minutes_before_open:
            return True
        if (
            run.current_state == DailyWorkflowState.PREMARKET_PREPARATION.value
            and mins <= self.settings.premarket_analysis_minutes_before_open
        ):
            return True
        return False

    async def catch_up_to_intraday(
        self,
        *,
        session_date: str | None = None,
        now: datetime | None = None,
        fake_llm: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        """Advance a late/incomplete session into INTRADAY when the market needs it."""
        self._broker_guard()
        now = now or datetime.now(UTC)
        run = await self.get_current(session_date)
        if run is None:
            prepared = await self.prepare(session_date=session_date, now=now)
            run = await self._require_run(session_date or prepared.get("session_date"))
        self._assert_not_blocked(run)

        if run.current_state in {
            DailyWorkflowState.INTRADAY.value,
            DailyWorkflowState.MARKET_OPEN.value,
        }:
            return {**self._run_dict(run), "catch_up": {"skipped": True, "reason": "already_ready"}}

        if not force and not self._should_catch_up_session(run, now):
            return {
                **self._run_dict(run),
                "catch_up": {"skipped": True, "reason": "not_near_or_in_session"},
            }

        meta = dict(run.metadata_json or {})
        last = meta.get("last_catch_up_at")
        if last and not force:
            try:
                ts = datetime.fromisoformat(str(last))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                gap_min = (now - ts).total_seconds() / 60.0
                if gap_min < max(5, self.settings.min_reevaluation_gap_minutes):
                    return {
                        **self._run_dict(run),
                        "catch_up": {"skipped": True, "reason": "catch_up_cooldown", "gap_min": gap_min},
                    }
            except ValueError:
                pass

        steps: list[str] = []
        meta["last_catch_up_at"] = now.isoformat()
        run.metadata_json = meta
        await self.session.flush()

        if run.current_state == DailyWorkflowState.PREMARKET_PREPARATION.value:
            await self.run_analysis(session_date=run.session_date, fake_llm=fake_llm, now=now)
            steps.append("analysis")
            run = await self._require_run(run.session_date)
        elif run.current_state == DailyWorkflowState.PREMARKET_ANALYSIS.value:
            await self.run_analysis(session_date=run.session_date, fake_llm=fake_llm, now=now)
            steps.append("analysis")
            run = await self._require_run(run.session_date)

        if run.current_state == DailyWorkflowState.PREOPEN_REVALIDATION.value:
            settled = await self.revalidate(
                session_date=run.session_date, fake_llm=fake_llm, now=now
            )
            steps.append("revalidate")
            return {
                **settled,
                "catch_up": {"skipped": False, "steps": steps, "reason": "advanced"},
            }

        run = await self._require_run(run.session_date)
        return {
            **self._run_dict(run),
            "catch_up": {"skipped": False, "steps": steps, "reason": "partial"},
        }

    async def evaluate_intraday(
        self,
        *,
        session_date: str | None = None,
        trigger: str = "interval",
        now: datetime | None = None,
        fake_llm: bool = False,
    ) -> dict[str, Any]:
        self._broker_guard()
        now = now or datetime.now(UTC)
        run = await self._require_run(session_date)
        self._assert_not_blocked(run)
        if run.current_state not in {
            DailyWorkflowState.INTRADAY.value,
            DailyWorkflowState.MARKET_OPEN.value,
        }:
            # Late start / stuck preopen: finish prep then continue as intraday.
            catch = await self.catch_up_to_intraday(
                session_date=run.session_date, now=now, fake_llm=fake_llm
            )
            run = await self._require_run(run.session_date)
            if run.current_state not in {
                DailyWorkflowState.INTRADAY.value,
                DailyWorkflowState.MARKET_OPEN.value,
            }:
                raise DailyWorkflowError(
                    f"intraday_not_allowed_from:{run.current_state}"
                    + (f":catch_up={catch.get('catch_up')}" if isinstance(catch, dict) else "")
                )

        status = self.calendar.get_market_status(now)
        result = IntradayEvalResult.NO_CHANGE
        reason = "ok"
        agent_result: dict[str, Any] | None = None
        force_close_result: dict[str, Any] | None = None
        meta = dict(run.metadata_json or {})
        last = meta.get("last_intraday_eval_at")
        if last:
            try:
                ts = datetime.fromisoformat(str(last))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                gap = (now - ts).total_seconds() / 60.0
                # Horizon-aware min gap for interval triggers.
                from app.models import PositionLifecycle
                from app.universe.reeval import global_reeval_gap_minutes
                from app.universe.service import UniverseService
                from sqlalchemy import select as sa_select

                open_syms = [
                    p.symbol
                    for p in (
                        await self.session.execute(
                            sa_select(PositionLifecycle).where(
                                PositionLifecycle.status.in_(["OPEN", "ADDING", "REDUCING"])
                            )
                        )
                    )
                    .scalars()
                    .all()
                ]
                try:
                    horizons = await UniverseService(
                        self.session, settings=self.settings
                    ).horizon_by_symbol()
                except Exception:  # noqa: BLE001
                    horizons = {}
                need_gap = global_reeval_gap_minutes(open_syms, horizons, self.settings)
                if gap < need_gap and trigger == "interval" and not status.in_force_close_window:
                    return {
                        **self._run_dict(run),
                        "intraday": {
                            "result": result.value,
                            "reason": "min_gap",
                            "skipped": True,
                            "need_gap_minutes": need_gap,
                            "gap_minutes": gap,
                        },
                    }
            except ValueError:
                pass

        if status.in_force_close_window or status.in_closing_window:
            result = IntradayEvalResult.NO_CHANGE
            reason = "closing_window_limit_new_analysis"
        elif run.intraday_reanalysis_count >= self.settings.max_intraday_reanalyses:
            result = IntradayEvalResult.PAUSE_TRADING
            reason = "max_intraday_reanalyses"
        elif trigger in {"volatility", "news_high_importance", "risk_change"}:
            result = IntradayEvalResult.REANALYZE
            reason = f"event:{trigger}"
        elif trigger == "stale_data":
            result = IntradayEvalResult.RISK_REVIEW_REQUIRED
            reason = "stale_data"
        elif trigger == "interval" and self.settings.enable_intraday_agent_reanalysis:
            # Unattended paper path: interval jobs drive CIO reanalysis (cooldown inside agents).
            result = IntradayEvalResult.REANALYZE
            reason = "interval_agent_reeval"

        if result == IntradayEvalResult.REANALYZE and self.settings.enable_intraday_agent_reanalysis:
            from app.intraday.agents import IntradayAgentService

            agent_result = await IntradayAgentService(
                self.session, settings=self.settings, controls=self.controls
            ).evaluate(
                fake_llm=fake_llm,
                parent_decision_id=run.latest_decision_id,
                bypass_cooldown=trigger != "interval",
            )
            if agent_result.get("skipped"):
                result = IntradayEvalResult.NO_CHANGE
                reason = str(agent_result.get("reason") or "agent_skipped")
            else:
                run.intraday_reanalysis_count = int(run.intraday_reanalysis_count) + 1
                meta["last_intraday_agent"] = {
                    "portfolio_action": agent_result.get("portfolio_action"),
                    "intent_count": agent_result.get("intent_count", 0),
                    "broker_orders_submitted": bool(agent_result.get("broker_orders_submitted")),
                    "mode": agent_result.get("mode"),
                }

        # Force-close window: materialize exits even when analysis is paused.
        if status.in_force_close_window:
            try:
                from app.intraday.closing import ClosingService

                force_close_result = await ClosingService(
                    self.session, settings=self.settings
                ).run_closing(in_closing_window=True)
                meta["last_force_close"] = {
                    "intent_ids": force_close_result.get("intent_ids") or [],
                    "orders_submitted": force_close_result.get("orders_submitted") or 0,
                    "notes": force_close_result.get("notes") or [],
                }
                reason = "force_close_run"
            except Exception as exc:  # noqa: BLE001
                meta["last_force_close_error"] = str(exc)
                reason = f"force_close_failed:{exc}"

        meta["last_intraday_eval_at"] = now.isoformat()
        meta["last_intraday_result"] = result.value
        run.metadata_json = meta
        if run.current_state == DailyWorkflowState.MARKET_OPEN.value:
            await self._set_state(
                run, DailyWorkflowState.INTRADAY, trigger="evaluate_intraday", reason="enter"
            )
        await self.session.flush()
        return {
            **self._run_dict(run),
            "intraday": {
                "result": result.value,
                "reason": reason,
                "broker_orders": bool(
                    (agent_result or {}).get("broker_orders_submitted")
                    or (force_close_result or {}).get("broker_orders_submitted")
                ),
                "agent": agent_result,
                "force_close": force_close_result,
            },
        }

    async def start_closing(
        self,
        *,
        session_date: str | None = None,
        policy: ClosingPolicy = ClosingPolicy.CLOSE_INTRADAY_ONLY,
        positions: list[dict[str, Any]] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        self._broker_guard()
        now = now or datetime.now(UTC)
        run = await self._require_run(session_date)
        self._assert_not_blocked(run)
        if run.current_state not in {
            DailyWorkflowState.INTRADAY.value,
            DailyWorkflowState.MARKET_OPEN.value,
            DailyWorkflowState.CLOSING_WINDOW.value,
        }:
            raise DailyWorkflowError(f"closing_not_allowed_from:{run.current_state}")
        if run.current_state != DailyWorkflowState.CLOSING_WINDOW.value:
            await self._set_state(
                run, DailyWorkflowState.CLOSING_WINDOW, trigger="start_closing", reason="enter"
            )

        # Horizon-aware force flatten + OrderIntents (optional paper submit).
        from app.intraday.closing import ClosingService

        closing_svc = await ClosingService(self.session, settings=self.settings).run_closing(
            in_closing_window=True
        )
        # Legacy policy shape for callers that only pass explicit positions.
        legacy = self.closing.decide(
            as_of=now,
            positions=positions or [],
            policy=policy,
            intraday_symbols=set(),
        ).to_dict()
        closing_payload = {
            **legacy,
            "policy": closing_svc.get("policy") or legacy.get("policy"),
            "plans": closing_svc.get("plans") or legacy.get("plans") or [],
            "notes": list(closing_svc.get("notes") or []) + list(legacy.get("notes") or []),
            "intent_ids": closing_svc.get("intent_ids") or [],
            "intent_drafts": closing_svc.get("intent_drafts") or [],
            "broker_orders_submitted": bool(closing_svc.get("broker_orders_submitted")),
            "orders_submitted": int(closing_svc.get("orders_submitted") or 0),
            "review_id": closing_svc.get("review_id"),
            "broker_orders_allowed": False,
        }
        meta = dict(run.metadata_json or {})
        meta["closing_decision"] = closing_payload
        run.metadata_json = meta
        await self.session.flush()
        return {**self._run_dict(run), "closing": closing_payload}

    async def run_postmarket(
        self, *, session_date: str | None = None, now: datetime | None = None
    ) -> dict[str, Any]:
        self._broker_guard()
        now = now or datetime.now(UTC)
        run = await self._require_run(session_date)
        self._assert_not_blocked(run)
        # Allow jump from closing → closed → postmarket
        if run.current_state == DailyWorkflowState.CLOSING_WINDOW.value:
            await self._set_state(
                run, DailyWorkflowState.MARKET_CLOSED, trigger="postmarket", reason="session_ended"
            )
        if run.current_state == DailyWorkflowState.MARKET_CLOSED.value:
            await self._set_state(
                run,
                DailyWorkflowState.POSTMARKET_REVIEW,
                trigger="postmarket",
                reason="start_review",
            )
        if run.current_state != DailyWorkflowState.POSTMARKET_REVIEW.value:
            raise DailyWorkflowError(f"postmarket_not_allowed_from:{run.current_state}")

        transitions = list(
            (
                await self.session.execute(
                    select(WorkflowStateTransition).where(
                        WorkflowStateTransition.workflow_run_id == run.id
                    )
                )
            )
            .scalars()
            .all()
        )
        review = {
            "session_date": run.session_date,
            "states": [t.to_state for t in transitions],
            "revalidation_count": run.revalidation_count,
            "intraday_reanalysis_count": run.intraday_reanalysis_count,
            "no_trade_reason": (run.metadata_json or {}).get("no_trade_reason"),
            "cio_action": (run.metadata_json or {}).get("cio_action"),
            "broker_orders_submitted": False,
            "reviewed_at": now.isoformat(),
        }
        meta = dict(run.metadata_json or {})
        meta["postmarket_review"] = review
        run.metadata_json = meta
        await self._set_state(
            run, DailyWorkflowState.COMPLETED, trigger="postmarket", reason="review_done"
        )
        run.status = WorkflowRunStatus.COMPLETED.value
        run.completed_at = now
        await self.session.flush()
        return {**self._run_dict(run), "review": review}

    async def list_transitions(self, workflow_run_id: Any) -> list[dict[str, Any]]:
        rows = list(
            (
                await self.session.execute(
                    select(WorkflowStateTransition)
                    .where(WorkflowStateTransition.workflow_run_id == workflow_run_id)
                    .order_by(WorkflowStateTransition.started_at)
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "from_state": r.from_state,
                "to_state": r.to_state,
                "trigger": r.trigger,
                "reason": r.reason,
                "started_at": r.started_at.isoformat(),
                "success": r.success,
                "error_code": r.error_code,
            }
            for r in rows
        ]

    async def planned_jobs(self, session_date: str | None = None) -> list[dict[str, Any]]:
        q = select(ScheduledJobRecord)
        if session_date:
            q = q.where(ScheduledJobRecord.session_date == session_date)
        rows = list((await self.session.execute(q.order_by(ScheduledJobRecord.planned_at))).scalars())
        return [
            {
                "job_key": r.job_key,
                "session_date": r.session_date,
                "planned_at": (
                    r.planned_at.replace(tzinfo=UTC) if r.planned_at.tzinfo is None else r.planned_at
                ).isoformat(),
                "status": r.status,
            }
            for r in rows
        ]

    async def _plan_jobs(self, run: DailyWorkflowRun, session: Any) -> None:
        if not session.is_trading_day or not session.regular_open or not session.regular_close:
            return
        open_t = session.regular_open
        close_t = session.regular_close
        cfg = self.settings
        plans = [
            (
                "premarket_preparation",
                open_t - timedelta(minutes=cfg.premarket_preparation_minutes_before_open),
            ),
            (
                "premarket_analysis",
                open_t - timedelta(minutes=cfg.premarket_analysis_minutes_before_open),
            ),
            (
                "preopen_revalidation",
                open_t - timedelta(minutes=cfg.preopen_revalidation_minutes_before_open),
            ),
            (
                "closing_window",
                close_t - timedelta(minutes=cfg.closing_window_minutes_before_close),
            ),
            (
                "postmarket_review",
                close_t + timedelta(minutes=cfg.postmarket_review_minutes_after_close),
            ),
        ]
        # Intraday interval jobs — denser when watchlist includes scalp/day books,
        # but floored by LLM reanalysis budget (≈ 2 × max_intraday_reanalyses ticks).
        end = close_t - timedelta(minutes=cfg.closing_window_minutes_before_close)
        session_mins = max(0.0, (end - open_t).total_seconds() / 60.0)
        try:
            from app.universe.reeval import planned_intraday_interval_minutes
            from app.universe.service import UniverseService

            hz_map = await UniverseService(self.session, settings=cfg).horizon_by_symbol()
            interval_min = planned_intraday_interval_minutes(
                list(hz_map.values()), cfg, session_minutes=session_mins
            )
        except Exception:  # noqa: BLE001
            interval_min = max(1, int(cfg.intraday_reevaluation_interval_minutes))
        cursor = open_t + timedelta(minutes=interval_min)
        idx = 0
        while cursor < end:
            plans.append((f"intraday_eval_{idx}", cursor))
            cursor += timedelta(minutes=interval_min)
            idx += 1

        for key, planned in plans:
            existing = (
                await self.session.execute(
                    select(ScheduledJobRecord).where(
                        ScheduledJobRecord.job_key == key,
                        ScheduledJobRecord.session_date == run.session_date,
                    )
                )
            ).scalar_one_or_none()
            if existing:
                continue
            meta: dict[str, Any] = {}
            if str(key).startswith("intraday_eval"):
                meta["interval_minutes"] = interval_min
            self.session.add(
                ScheduledJobRecord(
                    id=uuid4(),
                    job_key=key,
                    session_date=run.session_date,
                    planned_at=planned.astimezone(UTC),
                    status="planned",
                    workflow_run_id=run.id,
                    metadata_json=meta,
                )
            )
        await self.session.flush()

    async def _require_run(self, session_date: str | None) -> DailyWorkflowRun:
        run = await self.get_current(session_date)
        if run is None:
            raise DailyWorkflowError("daily_workflow_not_prepared")
        return run

    def _assert_not_blocked(self, run: DailyWorkflowRun) -> None:
        snap = self.controls.snapshot()
        if snap.state.value == "emergency_stop" or run.current_state == DailyWorkflowState.EMERGENCY_STOP.value:
            raise DailyWorkflowError("emergency_stop_active")
        if snap.state.value == "paused" or run.current_state == DailyWorkflowState.PAUSED.value:
            raise DailyWorkflowError("workflow_paused")

    async def _set_state(
        self,
        run: DailyWorkflowRun,
        to_state: DailyWorkflowState,
        *,
        trigger: str,
        reason: str,
    ) -> None:
        from_state = DailyWorkflowState(run.current_state)
        assert_transition_allowed(from_state, to_state)
        if not BROKER_ORDERS_ALLOWED[to_state]:
            pass  # explicit: never enable broker from state machine
        await self._transition(run, from_state, to_state, trigger=trigger, reason=reason)
        run.current_state = to_state.value
        run.version = int(run.version) + 1
        if to_state == DailyWorkflowState.FAILED:
            run.status = WorkflowRunStatus.FAILED.value
            run.failed_at = datetime.now(UTC)
            run.failure_reason = reason
        await self.session.flush()

    async def _transition(
        self,
        run: DailyWorkflowRun,
        from_state: DailyWorkflowState,
        to_state: DailyWorkflowState,
        *,
        trigger: str,
        reason: str,
        success: bool = True,
        error_code: str | None = None,
    ) -> None:
        now = datetime.now(UTC)
        self.session.add(
            WorkflowStateTransition(
                id=uuid4(),
                workflow_run_id=run.id,
                from_state=from_state.value,
                to_state=to_state.value,
                trigger=trigger,
                reason=reason,
                started_at=now,
                completed_at=now,
                success=success,
                error_code=error_code,
                metadata_json={},
            )
        )
        await self.session.flush()

    def _run_dict(self, run: DailyWorkflowRun, note: str | None = None) -> dict[str, Any]:
        payload = {
            "id": str(run.id),
            "session_date": run.session_date,
            "calendar_name": run.calendar_name,
            "current_state": run.current_state,
            "status": run.status,
            "early_close": run.early_close,
            "market_open_at": run.market_open_at.isoformat() if run.market_open_at else None,
            "market_close_at": run.market_close_at.isoformat() if run.market_close_at else None,
            "analysis_workflow_run_id": str(run.analysis_workflow_run_id)
            if run.analysis_workflow_run_id
            else None,
            "broker_orders_allowed": False,
            "enable_broker_orders": self.settings.enable_broker_orders,
            "metadata": run.metadata_json,
            "version": run.version,
        }
        if note:
            payload["note"] = note
        return payload
