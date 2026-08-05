"""Preopen revalidation (fixture/stub capable; no broker)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.market.calendar import MarketCalendarService
from app.models import DailyWorkflowRun, RevalidationRun
from app.workflow.states import RevalidationResult


@dataclass(slots=True)
class RevalidationReport:
    result: RevalidationResult
    reason: str
    attempt: int
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "result": self.result.value,
            "reason": self.reason,
            "attempt": self.attempt,
            "details": self.details,
        }


class RevalidationService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        calendar: MarketCalendarService | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.calendar = calendar or MarketCalendarService(self.settings)

    async def revalidate(
        self,
        run: DailyWorkflowRun,
        *,
        now: datetime | None = None,
        fixture: dict[str, Any] | None = None,
    ) -> RevalidationReport:
        now = now or datetime.now(UTC)
        attempt = int(run.revalidation_count) + 1
        fixture = fixture or {}
        status = self.calendar.get_market_status(now)
        details: dict[str, Any] = {"market_phase": status.phase, "session": status.session.to_dict()}

        if fixture.get("force_holiday"):
            report = RevalidationReport(
                result=RevalidationResult.NO_TRADE,
                reason="fixture_force_holiday",
                attempt=attempt,
                details=details,
            )
            await self._persist(run, report)
            return report

        if not status.is_trading_day:
            report = RevalidationReport(
                result=RevalidationResult.NO_TRADE,
                reason="non_trading_day",
                attempt=attempt,
                details=details,
            )
            await self._persist(run, report)
            return report

        # Explicit operator/test fixtures take precedence over TTL/event heuristics
        if fixture.get("hard_veto"):
            report = RevalidationReport(
                result=RevalidationResult.NO_TRADE,
                reason="hard_veto_fixture",
                attempt=attempt,
                details=details,
            )
            await self._persist(run, report)
            return report

        if fixture.get("material_conflict"):
            report = RevalidationReport(
                result=RevalidationResult.NO_TRADE,
                reason="material_data_conflict",
                attempt=attempt,
                details=details,
            )
            await self._persist(run, report)
            return report

        if fixture.get("high_importance_news"):
            report = RevalidationReport(
                result=RevalidationResult.REANALYSIS_REQUIRED,
                reason="high_importance_news",
                attempt=attempt,
                details=details,
            )
            await self._persist(run, report)
            return report

        if fixture.get("stale_data"):
            report = RevalidationReport(
                result=RevalidationResult.REANALYSIS_REQUIRED
                if attempt <= self.settings.max_revalidation_retries + 1
                else RevalidationResult.NO_TRADE,
                reason="stale_data_fixture",
                attempt=attempt,
                details=details,
            )
            await self._persist(run, report)
            return report

        meta = run.metadata_json or {}
        analysed_at = meta.get("analysis_completed_at")
        if analysed_at:
            try:
                ts = datetime.fromisoformat(str(analysed_at))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                age = now - ts
                ttl = timedelta(minutes=self.settings.analysis_decision_ttl_minutes)
                if age > ttl:
                    if attempt <= self.settings.max_revalidation_retries + 1:
                        report = RevalidationReport(
                            result=RevalidationResult.REANALYSIS_REQUIRED,
                            reason="analysis_expired",
                            attempt=attempt,
                            details={**details, "age_minutes": age.total_seconds() / 60.0},
                        )
                    else:
                        report = RevalidationReport(
                            result=RevalidationResult.NO_TRADE,
                            reason="revalidation_retries_exhausted",
                            attempt=attempt,
                            details=details,
                        )
                    await self._persist(run, report)
                    return report
            except ValueError:
                pass

        # Live metadata from prior collection — only NEW events after analysis cutoff
        events = meta.get("market_events") or []
        analysed_at = meta.get("analysis_completed_at")
        cutoff_ts: datetime | None = None
        if analysed_at:
            try:
                cutoff_ts = datetime.fromisoformat(str(analysed_at))
                if cutoff_ts.tzinfo is None:
                    cutoff_ts = cutoff_ts.replace(tzinfo=UTC)
            except ValueError:
                cutoff_ts = None

        def _is_new_event(ev: dict[str, Any]) -> bool:
            # Prefer detection time: effective_at can be days earlier (filings) and must
            # not keep forcing reanalysis forever after the event was already seen.
            if ev.get("incorporated_at") or ev.get("requires_reanalysis") is False:
                return False
            if cutoff_ts is None:
                return bool(ev.get("requires_reanalysis"))
            raw = ev.get("detected_at") or ev.get("effective_at")
            if not raw:
                return False
            try:
                ts = datetime.fromisoformat(str(raw))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                return ts > cutoff_ts
            except ValueError:
                return False

        if (
            any(e.get("requires_reanalysis") and _is_new_event(e) for e in events)
            and attempt <= self.settings.max_revalidation_retries
        ):
            report = RevalidationReport(
                result=RevalidationResult.REANALYSIS_REQUIRED,
                reason="market_event_requires_reanalysis",
                attempt=attempt,
                details={**details, "events": events[:5]},
            )
            await self._persist(run, report)
            return report

        if meta.get("data_fail_closed"):
            report = RevalidationReport(
                result=RevalidationResult.NO_TRADE,
                reason="data_fail_closed:" + ",".join(meta.get("data_fail_closed_reasons") or []),
                attempt=attempt,
                details=details,
            )
            await self._persist(run, report)
            return report

        if meta.get("cio_action") in {"NO_TRADE", "STAY_CASH"} and fixture.get("restrict"):
            report = RevalidationReport(
                result=RevalidationResult.VALID_WITH_RESTRICTIONS,
                reason="prior_no_trade_with_restrictions",
                attempt=attempt,
                details=details,
            )
            await self._persist(run, report)
            return report

        report = RevalidationReport(
            result=RevalidationResult.VALID,
            reason="ok",
            attempt=attempt,
            details=details,
        )
        await self._persist(run, report)
        return report

    async def _persist(self, run: DailyWorkflowRun, report: RevalidationReport) -> None:
        run.revalidation_count = int(run.revalidation_count) + 1
        self.session.add(
            RevalidationRun(
                id=uuid4(),
                workflow_run_id=run.id,
                result=report.result.value,
                reason=report.reason,
                attempt=report.attempt,
                payload=report.details,
            )
        )
        await self.session.flush()
