"""Daily workflow orchestration — 6-agent firm day cycle.

Trading authority: CIO bottom-up decision → Order Intents → paper broker when
safety flags unlock. Live trading stays blocked. Manual approval is an optional
ops brake, not the firm identity.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.pipeline import AgentPipeline
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.execution.safety_controls import TradingControls, trading_controls
from app.ingestion.pipeline import DataCollectionPipeline
from app.market.calendar import MarketCalendarService
from app.models import DailyWorkflowRun, ScheduledJobRecord, WorkflowStateTransition
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


# Postmarket is settlement + bookkeeping, not a committee call. Bound each
# step so a wedged IBKR/perf scan cannot eat the 8-minute job cap and leave
# the session stuck in POSTMARKET_REVIEW. Decision eval is split across
# follow-up postmarket_eval jobs — no practical count cap, just time slices.
POSTMARKET_STEP_TIMEOUTS_SECONDS: dict[str, float] = {
    "overnight_review": 20.0,
    "settlement": 60.0,
    "posttrade": 20.0,
    "force_close": 20.0,
    "performance": 60.0,
}
POSTMARKET_EVAL_CHUNK = 12
# After hours, chew several chunks per dispatch without hitting the 8-minute job cap.
POSTMARKET_EVAL_BUDGET_SECONDS = 240.0
POSTMARKET_EVAL_MAX_CHUNKS_PER_JOB = 40
POSTMARKET_EVAL_PHASES = {
    "POSTMARKET",
    "AFTER_HOURS",
    "NON_TRADING_DAY",
    "BEFORE_PREMARKET",
}


async def _await_postmarket_step(name: str, coro: Any) -> Any:
    timeout = float(POSTMARKET_STEP_TIMEOUTS_SECONDS.get(name) or 30.0)
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except TimeoutError:
        logger.error("postmarket_step_timeout", step=name, timeout_s=timeout)
        raise TimeoutError(f"timeout:{name}:{int(timeout)}s") from None


class DailyWorkflowService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        controls: TradingControls | None = None,
        owner: str = "daily-workflow",
        venue: str | None = None,
    ) -> None:
        from app.market.venues import resolve_venue, run_calendar_name

        self.session = session
        self.settings = settings or get_settings()
        self.controls = controls or trading_controls
        self.venue = resolve_venue(self.settings, venue=venue)
        self.run_calendar_name = run_calendar_name(self.venue, self.settings)
        self.calendar = MarketCalendarService(self.settings, venue=self.venue)
        self.leases = LeaseService(session, self.settings)
        self.revalidation = RevalidationService(session, settings=self.settings, calendar=self.calendar)
        self.closing = ClosingPolicyEngine()
        self.owner = owner

    def _jk(self, base: str) -> str:
        from app.market.venues import scoped_job_key

        return scoped_job_key(self.venue, base)

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
                    DailyWorkflowRun.calendar_name == self.run_calendar_name,
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
        lease_key = f"daily:{self.run_calendar_name}:{day.isoformat()}:prepare"
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
                    calendar_name=self.run_calendar_name,
                    current_state=DailyWorkflowState.NON_TRADING_DAY.value,
                    status=WorkflowRunStatus.COMPLETED.value,
                    started_at=now,
                    completed_at=now,
                    timezone=str(self.calendar.market_tz),
                    early_close=False,
                    metadata_json={"note": "non_trading_day", "venue": self.venue.value},
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
                calendar_name=self.run_calendar_name,
                current_state=DailyWorkflowState.PREMARKET_PREPARATION.value,
                status=WorkflowRunStatus.RUNNING.value,
                started_at=now,
                timezone=str(self.calendar.market_tz),
                market_open_at=session.regular_open,
                market_close_at=session.regular_close,
                early_close=session.is_early_close,
                metadata_json={"session": session.to_dict(), "venue": self.venue.value},
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
        lease_key = f"daily:{self.venue.value}:{run.session_date}:analysis"
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
            use_fixtures = not bool(self.settings.enable_external_data)
            if (
                use_fixtures
                and (
                    self.settings.enable_broker_orders
                    or self.settings.enable_automated_execution
                )
            ):
                raise DailyWorkflowError("external_data_required_when_execution_armed")
            from app.execution.position_manager import PositionManager
            from app.universe.service import UniverseService

            try:
                portfolio, portfolio_note = await PositionManager(
                    self.session, settings=self.settings
                ).load_for_risk()
            except Exception as exc:  # noqa: BLE001
                raise DailyWorkflowError(f"portfolio_sync_failed:{exc}") from exc

            from app.market.book_context import build_venue_book_context
            from app.market.venues import holdings_for_venue

            holdings = holdings_for_venue(
                list(portfolio.positions or []), self.venue, settings=self.settings
            )
            univ = UniverseService(self.session, settings=self.settings)
            collect_symbols = await univ.collection_universe(
                holdings=holdings, venue=self.venue.value
            )
            entry_universe = await univ.entry_universe(venue=self.venue.value)
            hz_map = await univ.horizon_by_symbol()
            status = self.calendar.get_market_status(now)
            book = build_venue_book_context(
                self.settings,
                venue=self.venue,
                session_date=run.session_date,
                phase=status.phase,
                allowlist=entry_universe,
            )

            data = await DataCollectionPipeline(
                self.session,
                settings=self.settings,
                fixture_mode=use_fixtures,
            ).collect(
                "PREMARKET",
                workflow_id=run.id,
                symbols=collect_symbols,
                venue=self.venue.value,
            )
            collection = data.legacy_bundle
            if collection is None:
                collection = await DataCollectionService(
                    self.session, settings=self.settings, persist=True
                ).collect_premarket(workflow_id=run.id, symbols=collect_symbols)
            if data.fail_closed:
                meta = dict(run.metadata_json or {})
                meta["data_fail_closed"] = True
                meta["data_fail_closed_reasons"] = data.fail_closed_reasons
                meta["collection_run_id"] = str(data.collection_run_id)
                meta["collection_symbols"] = collect_symbols
                meta["venue"] = self.venue.value
                run.metadata_json = meta
            else:
                meta = dict(run.metadata_json or {})
                meta["data_fail_closed"] = False
                meta["data_fail_closed_reasons"] = []
                meta["collection_run_id"] = str(data.collection_run_id)
                meta["collection_symbols"] = collect_symbols
                meta["venue"] = self.venue.value
                run.metadata_json = meta
            analysis = await AgentPipeline(settings=self.settings, llm=llm).run_from_collection(
                collection,
                portfolio=portfolio,
                proposed_trades=[],
                workflow_id=run.id,
                entry_universe=sorted(entry_universe),
                watchlist_context=[
                    {"symbol": s, "horizon": hz_map.get(s, "short")}
                    for s in sorted(entry_universe)
                ],
                book=book,
            )
            from app.services.audit import AuditService

            await AuditService(self.session).persist_analysis(analysis)
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
                entry_universe=entry_universe,
                horizon_by_symbol=hz_map,
                market_session_clear=not (data.fail_closed or collection.fail_closed),
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
                    "portfolio_source": portfolio_note,
                    "portfolio_equity": portfolio.equity,
                    "portfolio_cash": portfolio.cash,
                    "portfolio_positions": len(portfolio.positions),
                    "collection_fixture_mode": use_fixtures,
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
            mopped = await self._complete_planned_jobs(
                run.session_date,
                [self._jk("premarket_analysis"), self._jk("preopen_revalidation")],
                now=now,
                note="catch_up_already_ready",
            )
            return {
                **self._run_dict(run),
                "catch_up": {
                    "skipped": True,
                    "reason": "already_ready",
                    "jobs_marked": mopped,
                },
            }

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
            jobs_marked = await self._mark_catch_up_jobs(run.session_date, steps, now=now)
            return {
                **settled,
                "catch_up": {
                    "skipped": False,
                    "steps": steps,
                    "reason": "advanced",
                    "jobs_marked": jobs_marked,
                },
            }

        run = await self._require_run(run.session_date)
        jobs_marked = await self._mark_catch_up_jobs(run.session_date, steps, now=now)
        return {
            **self._run_dict(run),
            "catch_up": {
                "skipped": False,
                "steps": steps,
                "reason": "partial",
                "jobs_marked": jobs_marked,
            },
        }

    async def _mark_catch_up_jobs(
        self,
        session_date: str,
        steps: list[str],
        *,
        now: datetime,
    ) -> list[str]:
        keys: list[str] = []
        if "analysis" in steps:
            keys.append(self._jk("premarket_analysis"))
        if "revalidate" in steps:
            # Analysis already happened before revalidate in the catch-up path.
            if self._jk("premarket_analysis") not in keys:
                keys.append(self._jk("premarket_analysis"))
            keys.append(self._jk("preopen_revalidation"))
        return await self._complete_planned_jobs(
            session_date, keys, now=now, note="catch_up"
        )

    async def _complete_planned_jobs(
        self,
        session_date: str,
        job_keys: list[str],
        *,
        now: datetime,
        note: str,
    ) -> list[str]:
        """Mark matching planned/running jobs completed so catch-up cannot race the due loop."""
        marked: list[str] = []
        for key in job_keys:
            row = (
                await self.session.execute(
                    select(ScheduledJobRecord).where(
                        ScheduledJobRecord.session_date == session_date,
                        ScheduledJobRecord.job_key == key,
                        ScheduledJobRecord.status.in_(["planned", "running"]),
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                continue
            row.status = "completed"
            row.completed_at = now
            row.error = None
            if not row.started_at:
                row.started_at = now
            marked.append(key)
            logger.info(
                "scheduler_job_completed_by_catch_up",
                job=key,
                session_date=session_date,
                note=note,
            )
        if marked:
            await self.session.flush()
        return marked

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
        leftover_flatten_result: dict[str, Any] | None = None
        monitor_summary: dict[str, Any] | None = None
        news_summary: dict[str, Any] | None = None
        trigger_event_ids: list[str] = []
        effective_trigger = trigger
        meta = dict(run.metadata_json or {})
        try:
            from app.intraday.session_hygiene import fold_session_residue

            fold = await fold_session_residue(
                self.session,
                now=now,
                phase=status.phase,
                session_date=run.session_date,
            )
            if any(fold.values()):
                meta["last_session_fold"] = fold
        except Exception as exc:  # noqa: BLE001
            meta["last_session_fold_error"] = str(exc)[:200]

        # Ingest high-importance news onto the bus (works even if monitoring is off).
        try:
            from app.execution.position_manager import PositionManager
            from app.intraday.news_bridge import ingest_high_importance_news
            from app.market.venues import holdings_for_venue

            held_for_news: list[str] = []
            try:
                port_news = await PositionManager(
                    self.session, settings=self.settings
                ).portfolio_state_input()
                held_for_news = holdings_for_venue(
                    list(port_news.positions or []),
                    self.venue,
                    settings=self.settings,
                )
            except Exception:  # noqa: BLE001
                held_for_news = []
            news_summary = await ingest_high_importance_news(
                self.session,
                settings=self.settings,
                now=now,
                venue=self.venue.value,
                held_symbols=held_for_news,
            )
            meta["last_news_ingest"] = {
                "published": news_summary.get("published"),
                "scanned": news_summary.get("scanned"),
            }
        except Exception as exc:  # noqa: BLE001
            news_summary = {"error": str(exc)[:200]}
            meta["last_news_ingest_error"] = str(exc)[:200]

        # Safety: position monitor ticks on every unattended eval (before cooldown gate).
        pending: list[Any] = []
        actionable: list[dict[str, Any]] = []
        cio_actionable: list[dict[str, Any]] = []
        if self.settings.enable_intraday_monitoring:
            try:
                from sqlalchemy import select as sa_select

                from app.intraday.events import MONITOR_EXECUTED_EVENT_TYPES
                from app.intraday.monitor import (
                    ANALYSIS_REQUIRED,
                    EMERGENCY_ACTION_REQUIRED,
                    EXIT_INTENT_REQUIRED,
                    RISK_REVIEW_REQUIRED,
                )
                from app.intraday.service import IntradayService
                from app.models import PositionLifecycle

                intra = IntradayService(
                    self.session, settings=self.settings, controls=self.controls
                )
                mon_prices: dict[str, float] = {}
                try:
                    from app.market.live_prices import (
                        fetch_live_last_prices,
                        requires_live_market_prices,
                    )

                    if requires_live_market_prices(self.settings):
                        book = self.venue.value
                        open_rows = list(
                            (
                                await self.session.execute(
                                    sa_select(PositionLifecycle).where(
                                        PositionLifecycle.status.in_(
                                            ["OPEN", "ADDING", "REDUCING", "PENDING_CLOSE"]
                                        ),
                                        PositionLifecycle.venue == book,
                                    )
                                )
                            )
                            .scalars()
                            .all()
                        )
                        open_syms = [p.symbol for p in open_rows]
                        con_ids = {
                            p.symbol.upper(): int(p.con_id)
                            for p in open_rows
                            if getattr(p, "con_id", None)
                        }
                        if open_syms:
                            mon_prices = await fetch_live_last_prices(
                                open_syms, settings=self.settings, con_ids=con_ids or None
                            )
                except Exception as exc:  # noqa: BLE001
                    meta["last_monitor_price_error"] = str(exc)[:200]
                if mon_prices:
                    from app.market.paper_gates import paper_relaxed_data_gates

                    reasons = [
                        str(r)
                        for r in (meta.get("data_fail_closed_reasons") or [])
                        if r
                    ]
                    if (
                        paper_relaxed_data_gates(self.settings)
                        and meta.get("data_fail_closed")
                        and (not reasons or reasons == ["missing_core_index_data"])
                    ):
                        meta["data_fail_closed"] = False
                        meta["data_fail_closed_reasons"] = []
                        meta["data_fail_closed_cleared"] = "paper_live_quotes"
                mon_rows = await intra.monitor_all(
                    prices=mon_prices or None, venue=self.venue.value
                )
                escalate_verdicts = {
                    EXIT_INTENT_REQUIRED,
                    ANALYSIS_REQUIRED,
                    EMERGENCY_ACTION_REQUIRED,
                    RISK_REVIEW_REQUIRED,
                }
                cio_escalate = {
                    ANALYSIS_REQUIRED,
                    EMERGENCY_ACTION_REQUIRED,
                    RISK_REVIEW_REQUIRED,
                }
                actionable = [
                    r
                    for r in mon_rows
                    if not r.get("skipped")
                    and (
                        (r.get("monitor") or {}).get("verdict") in escalate_verdicts
                        or (r.get("stop") or {}).get("triggered")
                    )
                ]
                cio_actionable = [
                    r
                    for r in actionable
                    if (r.get("monitor") or {}).get("verdict") in cio_escalate
                ]
                pending = await intra.bus.list_pending_actionable(
                    limit=40, venue=self.venue.value
                )
                pending = [
                    e
                    for e in pending
                    if getattr(e, "event_type", "") not in MONITOR_EXECUTED_EVENT_TYPES
                ]
                trigger_event_ids = [str(e.id) for e in pending]
                monitor_summary = {
                    "checked": len([r for r in mon_rows if not r.get("skipped")]),
                    "actionable": len(actionable),
                    "pending_events": len(pending),
                    "verdicts": [
                        (r.get("monitor") or {}).get("verdict")
                        for r in mon_rows
                        if not r.get("skipped")
                    ],
                }
                meta["last_monitor"] = monitor_summary
            except Exception as exc:  # noqa: BLE001
                monitor_summary = {"error": str(exc)[:200]}
                meta["last_monitor_error"] = str(exc)[:200]
        else:
            # Still drain actionable bus events (e.g. news) when monitoring is off.
            try:
                from app.intraday.events import IntradayEventBus

                pending = await IntradayEventBus(
                    self.session, settings=self.settings
                ).list_pending_actionable(limit=40, venue=self.venue.value)
                trigger_event_ids = [str(e.id) for e in pending]
            except Exception as exc:  # noqa: BLE001
                meta["last_event_drain_error"] = str(exc)[:200]

        # Flatten leftover scalp/day: first regular tick after an overnight hold,
        # and again after the close if the force-close window was never ticked.
        missed_close = status.phase in {
            "POSTMARKET",
            "AFTER_HOURS",
            "FORCE_CLOSE_WINDOW",
            "CLOSING_WINDOW",
        }
        first_regular = status.phase == "REGULAR" and not status.in_force_close_window
        if (first_regular or missed_close) and not meta.get("leftover_intraday_flatten_at"):
            try:
                from app.intraday.closing import ClosingService

                leftover_flatten_result = await ClosingService(
                    self.session, settings=self.settings, venue=self.venue.value
                ).run_closing(in_closing_window=False)
                meta["leftover_intraday_flatten_at"] = now.isoformat()
                meta["leftover_intraday_flatten"] = {
                    "orders_submitted": int(
                        leftover_flatten_result.get("orders_submitted") or 0
                    ),
                    "intent_ids": list(leftover_flatten_result.get("intent_ids") or []),
                    "notes": list(leftover_flatten_result.get("notes") or [])[:12],
                }
            except Exception as exc:  # noqa: BLE001
                meta["leftover_intraday_flatten_error"] = str(exc)[:240]

        if cio_actionable:
            effective_trigger = "risk_change"
        elif pending:
            news_types = {"HIGH_IMPORTANCE_NEWS", "EARNINGS_RELEASE", "SEC_MATERIAL_FILING"}
            if pending and all(getattr(e, "event_type", "") in news_types for e in pending):
                effective_trigger = "news_high_importance"
            else:
                effective_trigger = "risk_change"
        elif (news_summary or {}).get("published"):
            # Published but already deduped as pending elsewhere — still escalate.
            effective_trigger = "news_high_importance"
            trigger_event_ids = list((news_summary or {}).get("event_ids") or [])

        last = meta.get("last_intraday_eval_at")
        if last:
            try:
                ts = datetime.fromisoformat(str(last))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                gap = (now - ts).total_seconds() / 60.0
                # Horizon-aware min gap for interval triggers.
                from sqlalchemy import select as sa_select

                from app.models import PositionLifecycle
                from app.universe.reeval import global_reeval_gap_minutes
                from app.universe.service import UniverseService

                open_syms = [
                    p.symbol
                    for p in (
                        await self.session.execute(
                            sa_select(PositionLifecycle).where(
                                PositionLifecycle.status.in_(["OPEN", "ADDING", "REDUCING"]),
                                PositionLifecycle.venue == self.venue.value,
                            )
                        )
                    )
                    .scalars()
                    .all()
                ]
                try:
                    univ = UniverseService(
                        self.session, settings=self.settings
                    )
                    horizons = await univ.horizon_by_symbol()
                    if not open_syms:
                        open_syms = await univ.collection_universe(
                            holdings=[], venue=self.venue.value
                        )
                except Exception:  # noqa: BLE001
                    horizons = {}
                from app.universe.book_strategy import is_active_strategy_horizon

                cadence_map = {
                    str(s).upper(): h
                    for s, h in (horizons or {}).items()
                    if is_active_strategy_horizon(h)
                }
                cadence_syms = [
                    str(s).upper()
                    for s in open_syms
                    if str(s).upper() in cadence_map
                ]
                need_gap = global_reeval_gap_minutes(
                    cadence_syms, cadence_map, self.settings
                )
                if (
                    gap < need_gap
                    and effective_trigger == "interval"
                    and not status.in_force_close_window
                ):
                    return {
                        **self._run_dict(run),
                        "intraday": {
                            "result": result.value,
                            "reason": "min_gap",
                            "skipped": True,
                            "need_gap_minutes": need_gap,
                            "gap_minutes": gap,
                            "monitor": monitor_summary,
                        },
                    }
            except ValueError:
                pass

        from app.universe.reeval import effective_max_intraday_reanalyses

        if status.in_force_close_window or status.in_closing_window:
            result = IntradayEvalResult.NO_CHANGE
            reason = "closing_window_limit_new_analysis"
        elif run.intraday_reanalysis_count >= effective_max_intraday_reanalyses(
            self.settings
        ):
            # Risk escalations still reanalyze even at the soft cap.
            if effective_trigger == "risk_change":
                result = IntradayEvalResult.REANALYZE
                reason = "event:risk_change_over_cap"
            else:
                result = IntradayEvalResult.PAUSE_TRADING
                reason = "max_intraday_reanalyses"
        elif effective_trigger in {"volatility", "news_high_importance", "risk_change"}:
            result = IntradayEvalResult.REANALYZE
            reason = f"event:{effective_trigger}"
        elif effective_trigger == "stale_data":
            result = IntradayEvalResult.RISK_REVIEW_REQUIRED
            reason = "stale_data"
        elif effective_trigger == "interval" and self.settings.enable_intraday_agent_reanalysis:
            # Unattended paper path: interval jobs drive CIO reanalysis (cooldown inside agents).
            result = IntradayEvalResult.REANALYZE
            reason = "interval_agent_reeval"

        from app.intraday.session_hygiene import committee_allowed_for_phase

        if result == IntradayEvalResult.REANALYZE and not committee_allowed_for_phase(
            status.phase,
            in_force_close=bool(status.in_force_close_window),
            in_closing=bool(status.in_closing_window),
        ):
            result = IntradayEvalResult.NO_CHANGE
            reason = f"committee_skipped_phase:{status.phase}"

        if result == IntradayEvalResult.REANALYZE and self.settings.enable_intraday_agent_reanalysis:
            from uuid import UUID as _UUID

            from app.intraday.agents import IntradayAgentService
            from app.intraday.events import IntradayEventBus

            agent_result = await IntradayAgentService(
                self.session, settings=self.settings, controls=self.controls
            ).evaluate(
                fake_llm=fake_llm,
                parent_decision_id=run.latest_decision_id,
                trigger_event_ids=trigger_event_ids or None,
                bypass_cooldown=effective_trigger != "interval",
                venue=self.venue.value,
                workflow_run=run,
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
                    "trigger": effective_trigger,
                    "trigger_event_ids": trigger_event_ids,
                }
                bus = IntradayEventBus(self.session, settings=self.settings)
                for eid in trigger_event_ids:
                    try:
                        await bus.mark(_UUID(eid), "PROCESSED")
                    except Exception:  # noqa: BLE001
                        continue

        # Force-close window: materialize exits even when analysis is paused.
        if status.in_force_close_window:
            try:
                from app.intraday.closing import ClosingService

                force_close_result = await ClosingService(
                    self.session, settings=self.settings, venue=self.venue.value
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
        if monitor_summary is not None:
            meta["last_monitor"] = monitor_summary
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
                "trigger": effective_trigger,
                "broker_orders": bool(
                    (agent_result or {}).get("broker_orders_submitted")
                    or (force_close_result or {}).get("broker_orders_submitted")
                    or (leftover_flatten_result or {}).get("broker_orders_submitted")
                ),
                "agent": agent_result,
                "force_close": force_close_result,
                "leftover_flatten": leftover_flatten_result,
                "monitor": monitor_summary,
                "news": news_summary,
            },
        }

    async def retry_missed_session_exits(
        self, *, now: datetime | None = None, session_date: str | None = None
    ) -> dict[str, Any]:
        """After the close, stamp missing stops and submit day/scalp flatten.

        ``evaluate_intraday`` is not allowed from CLOSING_WINDOW, and intraday
        jobs used to stop 30m before the close — so force-close never ran.
        Scheduler calls this on each venue after catch-up.
        """
        now = now or datetime.now(UTC)
        if session_date is None:
            session_date = now.astimezone(self.calendar.market_tz).date().isoformat()
        run = await self.get_current(session_date)
        if run is None:
            return {"skipped": True, "reason": "no_run"}
        if run.current_state not in {
            DailyWorkflowState.CLOSING_WINDOW.value,
            DailyWorkflowState.MARKET_CLOSED.value,
            DailyWorkflowState.INTRADAY.value,
            DailyWorkflowState.MARKET_OPEN.value,
        }:
            return {"skipped": True, "reason": f"state:{run.current_state}"}
        status = self.calendar.get_market_status(now)
        if status.phase not in {
            "POSTMARKET",
            "AFTER_HOURS",
            "FORCE_CLOSE_WINDOW",
            "CLOSING_WINDOW",
        }:
            return {"skipped": True, "reason": f"phase:{status.phase}"}
        meta = dict(run.metadata_json or {})
        last = meta.get("after_hours_flatten_at")
        if last:
            try:
                ts = datetime.fromisoformat(str(last))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                if (now - ts).total_seconds() < 15 * 60:
                    return {"skipped": True, "reason": "cooldown"}
            except ValueError:
                pass
        from sqlalchemy import select as sa_select

        from app.intraday.closing import ClosingService
        from app.intraday.service import IntradayService
        from app.models import PositionLifecycle

        open_rows = list(
            (
                await self.session.execute(
                    sa_select(PositionLifecycle).where(
                        PositionLifecycle.status.in_(
                            ["OPEN", "ADDING", "REDUCING", "PENDING_CLOSE"]
                        ),
                        PositionLifecycle.venue == self.venue.value,
                    )
                )
            )
            .scalars()
            .all()
        )
        if not open_rows:
            return {"skipped": True, "reason": "flat"}
        intra = IntradayService(
            self.session, settings=self.settings, controls=self.controls
        )
        monitor_rows = await intra.monitor_all(venue=self.venue.value)
        closing = await ClosingService(
            self.session, settings=self.settings, venue=self.venue.value
        ).run_closing(in_closing_window=False)
        submitted = int(closing.get("orders_submitted") or 0) + sum(
            int(r.get("orders_submitted") or 0) for r in monitor_rows if isinstance(r, dict)
        )
        meta["after_hours_flatten_at"] = now.isoformat()
        meta["last_force_close"] = {
            "intent_ids": list(closing.get("intent_ids") or []),
            "orders_submitted": submitted,
            "notes": list(closing.get("notes") or [])[:12],
            "source": "retry_missed_session_exits",
        }
        run.metadata_json = meta
        await self.session.flush()
        return {
            "skipped": False,
            "orders_submitted": submitted,
            "intent_ids": list(closing.get("intent_ids") or []),
            "monitor": [
                {
                    "symbol": r.get("symbol"),
                    "verdict": (r.get("monitor") or {}).get("verdict"),
                    "orders_submitted": r.get("orders_submitted") or 0,
                }
                for r in monitor_rows
                if isinstance(r, dict) and not r.get("skipped")
            ],
            "notes": list(closing.get("notes") or [])[:12],
        }

    async def retry_incomplete_postmarket(
        self, *, now: datetime | None = None, session_date: str | None = None
    ) -> dict[str, Any]:
        """Complete a session that closed but never finished postmarket review.

        Settlement flush errors used to abort the scheduler session and leave
        the run in CLOSING_WINDOW with the postmarket job stuck running.
        Catch-up calls this after hours so the book can still settle. When
        ``session_date`` is omitted, also try yesterday — a stuck AU close
        must not be abandoned just because the next premarket has started.
        """
        now = now or datetime.now(UTC)
        market_day = now.astimezone(self.calendar.market_tz).date()
        if session_date is not None:
            dates = [session_date]
        else:
            dates = [market_day.isoformat(), (market_day - timedelta(days=1)).isoformat()]
        last: dict[str, Any] = {"skipped": True, "reason": "no_run"}
        for day in dates:
            last = await self._retry_incomplete_postmarket_one(session_date=day, now=now)
            if not last.get("skipped"):
                return last
        return last

    async def _retry_incomplete_postmarket_one(
        self, *, session_date: str, now: datetime
    ) -> dict[str, Any]:
        run = await self.get_current(session_date)
        if run is None:
            return {"skipped": True, "reason": "no_run"}
        if run.current_state not in {
            DailyWorkflowState.CLOSING_WINDOW.value,
            DailyWorkflowState.MARKET_CLOSED.value,
            DailyWorkflowState.POSTMARKET_REVIEW.value,
        }:
            return {"skipped": True, "reason": f"state:{run.current_state}"}
        market_day = now.astimezone(self.calendar.market_tz).date().isoformat()
        prior_session = session_date < market_day
        if not prior_session:
            status = self.calendar.get_market_status(now)
            if status.phase not in POSTMARKET_EVAL_PHASES:
                return {"skipped": True, "reason": f"phase:{status.phase}"}
        meta = dict(run.metadata_json or {})
        last = meta.get("last_postmarket_retry_at")
        if last:
            try:
                ts = datetime.fromisoformat(str(last))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                if (now - ts).total_seconds() < 5 * 60:
                    return {"skipped": True, "reason": "cooldown"}
            except ValueError:
                pass
        meta["last_postmarket_retry_at"] = now.isoformat()
        run.metadata_json = meta
        await self.session.flush()
        out = await self.run_postmarket(session_date=run.session_date, now=now)
        from sqlalchemy import update as sa_update

        await self.session.execute(
            sa_update(ScheduledJobRecord)
            .where(ScheduledJobRecord.session_date == run.session_date)
            .where(ScheduledJobRecord.job_key == self._jk("postmarket_review"))
            .where(ScheduledJobRecord.status.in_(["failed", "running", "skipped"]))
            .values(status="completed", error=None, completed_at=now)
        )
        return {**out, "skipped": False, "recovered": True}

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

        closing_svc = await ClosingService(
            self.session, settings=self.settings, venue=self.venue.value
        ).run_closing(in_closing_window=True)
        overnight_payload: dict[str, Any] = {"reviews": []}
        try:
            from datetime import date as date_cls

            from app.alerts.ops import emit_overnight_review_alert

            session_day = date_cls.fromisoformat(run.session_date)
            holiday_gap = self.calendar.next_session_has_holiday_gap(session_day)
            overnight_payload = await ClosingService(
                self.session, settings=self.settings, venue=self.venue.value
            ).overnight_review(next_session_holiday=holiday_gap)
            overnight_payload["next_session_holiday"] = holiday_gap
            await emit_overnight_review_alert(
                self.session,
                self.settings,
                reviews=list(overnight_payload.get("reviews") or []),
                session_date=run.session_date,
            )
        except Exception as exc:  # noqa: BLE001
            overnight_payload = {"error": str(exc)[:240], "reviews": []}

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
            "overnight_review": overnight_payload,
            "broker_orders_allowed": False,
        }
        meta = dict(run.metadata_json or {})
        meta["closing_decision"] = closing_payload
        meta["last_force_close"] = {
            "intent_ids": closing_payload.get("intent_ids") or [],
            "orders_submitted": int(closing_payload.get("orders_submitted") or 0),
            "notes": list(closing_payload.get("notes") or [])[:12],
        }
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
        review: dict[str, Any] = {
            "session_date": run.session_date,
            "states": [t.to_state for t in transitions],
            "revalidation_count": run.revalidation_count,
            "intraday_reanalysis_count": run.intraday_reanalysis_count,
            "no_trade_reason": (run.metadata_json or {}).get("no_trade_reason"),
            "cio_action": (run.metadata_json or {}).get("cio_action"),
            "broker_orders_submitted": False,
            "reviewed_at": now.isoformat(),
        }

        # Settlement + light performance eval (fail-soft; never block session complete).
        # Each step is time-bounded so IBKR/perf cannot pin the scheduler job.
        prior_overnight = ((run.metadata_json or {}).get("closing") or {}).get(
            "overnight_review"
        )
        if isinstance(prior_overnight, dict) and prior_overnight.get("reviews") is not None:
            review["overnight_review"] = prior_overnight
            review["overnight_review_reused"] = True
        else:
            try:
                from datetime import date as date_cls

                from app.alerts.ops import emit_overnight_review_alert
                from app.intraday.closing import ClosingService

                session_day = date_cls.fromisoformat(run.session_date)
                holiday_gap = self.calendar.next_session_has_holiday_gap(session_day)

                async def _overnight() -> dict[str, Any]:
                    overnight = await ClosingService(
                        self.session, settings=self.settings, venue=self.venue.value
                    ).overnight_review(next_session_holiday=holiday_gap)
                    overnight["next_session_holiday"] = holiday_gap
                    await emit_overnight_review_alert(
                        self.session,
                        self.settings,
                        reviews=list(overnight.get("reviews") or []),
                        session_date=run.session_date,
                    )
                    return overnight

                review["overnight_review"] = await _await_postmarket_step(
                    "overnight_review", _overnight()
                )
            except TimeoutError as exc:
                review["overnight_review_error"] = str(exc)[:240]
            except Exception as exc:  # noqa: BLE001
                review["overnight_review_error"] = str(exc)[:240]

        try:
            from app.intraday.settlement import SettlementService

            async def _settle() -> dict[str, Any]:
                async with self.session.begin_nested():
                    return await SettlementService(
                        self.session, settings=self.settings, venue=self.venue.value
                    ).settle(session_date=run.session_date, venue=self.venue.value)

            settlement = await _await_postmarket_step("settlement", _settle())
            review["settlement"] = {
                "id": settlement.get("settlement_id"),
                "overnight_positions": settlement.get("overnight_positions") or [],
                "pnl_summary": settlement.get("pnl") or [],
                "reconciliation": (settlement.get("reconciliation") or {}).get("result")
                if isinstance(settlement.get("reconciliation"), dict)
                else settlement.get("reconciliation"),
            }
        except TimeoutError as exc:
            review["settlement_error"] = str(exc)[:240]
        except Exception as exc:  # noqa: BLE001
            review["settlement_error"] = str(exc)[:240]

        try:
            from sqlalchemy import select as sa_select

            from app.intraday.posttrade import PostTradeReviewService
            from app.models import PositionLifecycle

            day_start = datetime.fromisoformat(run.session_date).replace(tzinfo=UTC)
            day_end = day_start + timedelta(days=1)

            async def _posttrade() -> list[str]:
                closed = list(
                    (
                        await self.session.execute(
                            sa_select(PositionLifecycle).where(
                                PositionLifecycle.status.in_(["CLOSED", "PENDING_CLOSE"]),
                                PositionLifecycle.venue == self.venue.value,
                                PositionLifecycle.updated_at >= day_start,
                                PositionLifecycle.updated_at < day_end,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                ptr = PostTradeReviewService(self.session)
                review_ids: list[str] = []
                for lc in closed[:20]:
                    out = await ptr.create_review(
                        position_lifecycle_id=lc.id,
                        decision_id=lc.decision_id,
                        symbol=lc.symbol,
                        outcome="closed" if lc.status == "CLOSED" else "pending_close",
                        exit_reason="postmarket_review",
                        pnl=float(lc.realized_pl or lc.unrealized_pl or 0),
                    )
                    if out.get("review_id"):
                        review_ids.append(str(out["review_id"]))
                return review_ids

            review["posttrade_review_ids"] = await _await_postmarket_step(
                "posttrade", _posttrade()
            )
        except TimeoutError as exc:
            review["posttrade_error"] = str(exc)[:240]
        except Exception as exc:  # noqa: BLE001
            review["posttrade_error"] = str(exc)[:240]

        try:
            from app.intraday.closing import ClosingService

            async def _force_close_retry() -> dict[str, Any]:
                return await ClosingService(
                    self.session, settings=self.settings, venue=self.venue.value
                ).run_closing(in_closing_window=False)

            fc = await _await_postmarket_step("force_close", _force_close_retry())
            review["force_close"] = {
                "orders_submitted": int(fc.get("orders_submitted") or 0),
                "intent_ids": list(fc.get("intent_ids") or []),
                "notes": list(fc.get("notes") or [])[:12],
            }
        except TimeoutError as exc:
            review["force_close_error"] = str(exc)[:240]
        except Exception as exc:  # noqa: BLE001
            review["force_close_error"] = str(exc)[:240]

        try:
            from app.intraday.session_hygiene import fold_session_residue

            review["session_fold"] = await fold_session_residue(
                self.session,
                now=now,
                phase=str(self.calendar.get_market_status(now).phase or ""),
                session_date=run.session_date,
            )
        except Exception as exc:  # noqa: BLE001
            review["session_fold_error"] = str(exc)[:240]

        meta = dict(run.metadata_json or {})
        meta["postmarket_review"] = review
        run.metadata_json = meta
        await self._set_state(
            run, DailyWorkflowState.COMPLETED, trigger="postmarket", reason="review_done"
        )
        run.status = WorkflowRunStatus.COMPLETED.value
        run.completed_at = now
        queued = await self._enqueue_postmarket_eval(run, planned_at=now)
        review["decision_eval"] = {
            "queued": queued,
            "chunk": POSTMARKET_EVAL_CHUNK,
        }
        meta = dict(run.metadata_json or {})
        meta["postmarket_review"] = review
        run.metadata_json = meta
        await self.session.flush()
        return {**self._run_dict(run), "review": review}

    async def run_postmarket_eval(
        self,
        *,
        session_date: str | None = None,
        seq: int = 0,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Score this venue's leftover CIO rows until the time budget is gone.

        ``seq`` is ignored (legacy ``postmarket_eval_N`` keys still call this).
        Remaining work is signaled via ``eval.reschedule`` so the same job row
        is flipped back to ``planned``.
        """
        now = now or datetime.now(UTC)
        run = await self._require_run(session_date)
        self._assert_not_blocked(run)
        lookback = max(1, int(self.settings.decision_eval_lookback_days or 90))
        eval_end = now
        eval_start = eval_end - timedelta(days=lookback)
        chunk = POSTMARKET_EVAL_CHUNK
        payload: dict[str, Any] = {
            "chunk": chunk,
            "decision_eval_lookback_days": lookback,
            "reschedule": False,
            "delay_s": 0,
        }
        phase = str(self.calendar.get_market_status(now).phase or "")
        payload["phase"] = phase
        if phase not in POSTMARKET_EVAL_PHASES:
            payload["skipped"] = f"session_phase:{phase}"
            payload["reschedule"] = True
            payload["delay_s"] = 60
            meta = dict(run.metadata_json or {})
            meta["postmarket_eval"] = payload
            run.metadata_json = meta
            await self.session.flush()
            return {**self._run_dict(run), "eval": payload}

        from datetime import date as date_cls

        from app.performance.service import PerformanceService

        day = date_cls.fromisoformat(run.session_date)
        start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        end = start + timedelta(days=1)
        book = self.venue.value
        prior = dict((run.metadata_json or {}).get("postmarket_eval") or {})
        pending_refreshed = bool(prior.get("pending_refreshed"))
        recalc_id = prior.get("run_id")
        perf = PerformanceService(self.session, settings=self.settings)
        loop = asyncio.get_running_loop()
        budget = float(POSTMARKET_EVAL_BUDGET_SECONDS)
        deadline = loop.time() + max(5.0, budget)
        max_chunks = max(1, int(POSTMARKET_EVAL_MAX_CHUNKS_PER_JOB))
        total_processed = 0
        remaining = 0
        chunks_run = 0
        timed_out = False

        while chunks_run < max_chunks and loop.time() < deadline:
            slice_timeout = max(1.0, min(60.0, deadline - loop.time()))

            async def _chunk() -> dict[str, Any]:
                nonlocal pending_refreshed, recalc_id
                out: dict[str, Any] = {}
                if not pending_refreshed:
                    await perf.refresh_pending_evaluations(
                        eval_start, eval_end, venue=book
                    )
                    pending_refreshed = True
                    out["pending_refreshed"] = True
                if not recalc_id:
                    perf_run = await perf.recalculate(start, end)
                    recalc_id = perf_run.get("run_id")
                out["run_id"] = recalc_id
                decisions = await perf.evaluate_decisions_batch(
                    eval_start,
                    eval_end,
                    limit=chunk,
                    persist=True,
                    skip_evaluated=True,
                    venue=book,
                )
                out["decision_evaluations"] = int(decisions.get("count") or 0)
                out["decisions_processed"] = int(decisions.get("decisions_processed") or 0)
                out["remaining_decisions"] = int(decisions.get("remaining_decisions") or 0)
                return out

            try:
                piece = await asyncio.wait_for(_chunk(), timeout=slice_timeout)
            except TimeoutError as exc:
                timed_out = True
                payload["error"] = (
                    str(exc)[:240]
                    if str(exc)
                    else f"timeout:performance:{int(slice_timeout)}s"
                )
                remaining = max(remaining, 1)
                break
            except Exception as exc:  # noqa: BLE001
                payload["error"] = str(exc)[:240]
                break
            chunks_run += 1
            processed = int(piece.get("decisions_processed") or 0)
            remaining = int(piece.get("remaining_decisions") or 0)
            total_processed += processed
            payload["run_id"] = piece.get("run_id")
            payload["pending_refreshed"] = True
            if remaining <= 0 or processed <= 0:
                if remaining > 0 and processed <= 0:
                    logger.warning(
                        "postmarket_eval_stalled",
                        remaining=remaining,
                        error=payload.get("error"),
                        venue=book,
                    )
                break

        payload["decisions_processed"] = total_processed
        payload["remaining_decisions"] = remaining
        payload["chunks"] = chunks_run
        if remaining > 0 and (total_processed > 0 or timed_out):
            payload["reschedule"] = True
            payload["delay_s"] = 0
        payload["next_job"] = self._jk("postmarket_eval") if payload["reschedule"] else None

        meta = dict(run.metadata_json or {})
        history = list(meta.get("postmarket_evals") or [])
        history.append({k: v for k, v in payload.items() if k != "evaluations"})
        meta["postmarket_evals"] = history[-20:]
        meta["postmarket_eval"] = payload
        run.metadata_json = meta
        await self.session.flush()
        return {**self._run_dict(run), "eval": payload}

    async def _enqueue_postmarket_eval(
        self,
        run: DailyWorkflowRun,
        *,
        planned_at: datetime | None = None,
    ) -> str | None:
        key = self._jk("postmarket_eval")
        when = planned_at or datetime.now(UTC)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        when = when.astimezone(UTC)
        existing = (
            await self.session.execute(
                select(ScheduledJobRecord).where(
                    ScheduledJobRecord.job_key == key,
                    ScheduledJobRecord.session_date == run.session_date,
                )
            )
        ).scalar_one_or_none()
        if existing:
            if existing.status in {"completed", "failed", "skipped"}:
                existing.status = "planned"
                existing.planned_at = when
                existing.started_at = None
                existing.completed_at = None
                existing.error = None
                await self.session.flush()
            return key
        self.session.add(
            ScheduledJobRecord(
                id=uuid4(),
                job_key=key,
                session_date=run.session_date,
                planned_at=when,
                status="planned",
                workflow_run_id=run.id,
                metadata_json={"kind": "postmarket_eval"},
            )
        )
        await self.session.flush()
        return key

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
                self._jk("premarket_preparation"),
                open_t - timedelta(minutes=cfg.premarket_preparation_minutes_before_open),
            ),
            (
                self._jk("premarket_analysis"),
                open_t - timedelta(minutes=cfg.premarket_analysis_minutes_before_open),
            ),
            (
                self._jk("preopen_revalidation"),
                open_t - timedelta(minutes=cfg.preopen_revalidation_minutes_before_open),
            ),
            (
                self._jk("closing_window"),
                close_t - timedelta(minutes=cfg.closing_window_minutes_before_close),
            ),
            (
                self._jk("postmarket_review"),
                close_t + timedelta(minutes=cfg.postmarket_review_minutes_after_close),
            ),
        ]
        # Intraday interval jobs — denser for scalp/day; cloud floors by LLM
        # spend cap, local/embedded follows horizon cadence.
        await self._plan_intraday_jobs(run, session)

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
            self.session.add(
                ScheduledJobRecord(
                    id=uuid4(),
                    job_key=key,
                    session_date=run.session_date,
                    planned_at=planned.astimezone(UTC),
                    status="planned",
                    workflow_run_id=run.id,
                    metadata_json={},
                )
            )
        await self.session.flush()

    async def _plan_intraday_jobs(
        self,
        run: DailyWorkflowRun,
        session: Any,
        *,
        not_before: datetime | None = None,
    ) -> int:
        """Plan ``intraday_eval_*`` rows; returns count created."""
        if not session.is_trading_day or not session.regular_open or not session.regular_close:
            return 0
        open_t = session.regular_open
        close_t = session.regular_close
        cfg = self.settings
        # Include the force-close window. CIO analysis is already skipped there;
        # previously jobs stopped 30m before close so last_force_close never ran.
        end = close_t
        session_mins = max(0.0, (end - open_t).total_seconds() / 60.0)
        try:
            from sqlalchemy import select as sa_select

            from app.models import PositionLifecycle
            from app.universe.reeval import planned_intraday_interval_minutes
            from app.universe.service import UniverseService

            univ = UniverseService(self.session, settings=cfg)
            hz_map = await univ.horizon_by_symbol()
            book = self.venue.value
            open_syms = [
                p.symbol.upper()
                for p in (
                    await self.session.execute(
                        sa_select(PositionLifecycle).where(
                            PositionLifecycle.status.in_(["OPEN", "ADDING", "REDUCING"]),
                            PositionLifecycle.venue == book,
                        )
                    )
                )
                .scalars()
                .all()
            ]
            # Cadence follows open books when invested; otherwise focus/entry set —
            # not the entire watchlist (one scalp name must not force dense ticks
            # on a medium-only book).
            if open_syms:
                plan_horizons = [hz_map[s] for s in open_syms if s in hz_map]
            else:
                focus_syms = await univ.collection_universe(
                    holdings=[], venue=self.venue.value
                )
                plan_horizons = [hz_map[s] for s in focus_syms if s in hz_map]
            from app.universe.book_strategy import filter_strategy_horizons

            plan_horizons = filter_strategy_horizons(plan_horizons)
            if not plan_horizons:
                plan_horizons = filter_strategy_horizons(list(hz_map.values())) or ["day"]
            interval_min = planned_intraday_interval_minutes(
                plan_horizons, cfg, session_minutes=session_mins
            )
        except Exception:  # noqa: BLE001
            interval_min = max(1, int(cfg.intraday_reevaluation_interval_minutes))

        existing = list(
            (
                await self.session.execute(
                    select(ScheduledJobRecord).where(
                        ScheduledJobRecord.session_date == run.session_date,
                        ScheduledJobRecord.job_key.like(self._jk("intraday_eval_") + "%"),
                    )
                )
            )
            .scalars()
            .all()
        )
        max_idx = -1
        for row in existing:
            try:
                max_idx = max(max_idx, int(str(row.job_key).rsplit("_", 1)[-1]))
            except ValueError:
                continue

        cursor = open_t + timedelta(minutes=interval_min)
        if not_before is not None:
            nb = not_before if not_before.tzinfo else not_before.replace(tzinfo=UTC)
            while cursor < nb:
                cursor += timedelta(minutes=interval_min)

        idx = max_idx + 1
        created = 0
        while cursor < end:
            key = self._jk(f"intraday_eval_{idx}")
            collision = next((r for r in existing if r.job_key == key), None)
            if collision is None:
                self.session.add(
                    ScheduledJobRecord(
                        id=uuid4(),
                        job_key=key,
                        session_date=run.session_date,
                        planned_at=cursor.astimezone(UTC),
                        status="planned",
                        workflow_run_id=run.id,
                        metadata_json={
                            "interval_minutes": interval_min,
                            "replanned": bool(not_before),
                            "venue": self.venue.value,
                        },
                    )
                )
                created += 1
            idx += 1
            cursor += timedelta(minutes=interval_min)
        force_mins = max(1, int(cfg.force_close_before_market_close_minutes or 15))
        force_at = close_t - timedelta(minutes=min(5, force_mins))
        if open_t < force_at < close_t:
            covered = any(
                (
                    (
                        r.planned_at.replace(tzinfo=UTC)
                        if r.planned_at.tzinfo is None
                        else r.planned_at
                    )
                    >= force_at.astimezone(UTC)
                    and (
                        r.planned_at.replace(tzinfo=UTC)
                        if r.planned_at.tzinfo is None
                        else r.planned_at
                    )
                    < close_t.astimezone(UTC)
                )
                for r in existing
            )
            if not covered:
                key = self._jk(f"intraday_eval_{idx}")
                if next((r for r in existing if r.job_key == key), None) is None:
                    self.session.add(
                        ScheduledJobRecord(
                            id=uuid4(),
                            job_key=key,
                            session_date=run.session_date,
                            planned_at=force_at.astimezone(UTC),
                            status="planned",
                            workflow_run_id=run.id,
                            metadata_json={
                                "interval_minutes": interval_min,
                                "force_close_tick": True,
                                "venue": self.venue.value,
                            },
                        )
                    )
                    created += 1
        await self.session.flush()
        return created

    async def replan_intraday_jobs(
        self,
        session_date: str | None = None,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Drop pending intraday_eval rows and replan from current horizon cadence."""
        now = now or datetime.now(UTC)
        run = await self.get_current(session_date)
        if run is None:
            return {"skipped": True, "reason": "no_run"}
        session_info = self.calendar.get_session(date.fromisoformat(run.session_date))
        if not session_info.is_trading_day or not session_info.regular_open:
            return {"skipped": True, "reason": "non_trading_day", "session_date": run.session_date}

        purged = (
            await self.session.execute(
                delete(ScheduledJobRecord).where(
                    ScheduledJobRecord.session_date == run.session_date,
                    ScheduledJobRecord.job_key.like(self._jk("intraday_eval_") + "%"),
                    ScheduledJobRecord.status.in_(["planned", "skipped"]),
                )
            )
        ).rowcount or 0

        created = await self._plan_intraday_jobs(run, session_info, not_before=now)
        meta = dict(run.metadata_json or {})
        meta["last_intraday_replan"] = {
            "at": now.isoformat(),
            "purged": int(purged),
            "created": int(created),
        }
        run.metadata_json = meta
        await self.session.flush()
        return {
            "skipped": False,
            "session_date": run.session_date,
            "purged": int(purged),
            "created": int(created),
            "interval_hint": (meta.get("last_intraday_replan") or {}),
        }

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
            "venue": self.venue.value,
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
