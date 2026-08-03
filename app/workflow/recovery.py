"""Startup recovery for daily workflows (no broker)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.execution.ops_persistence import restore_trading_controls
from app.execution.safety_controls import trading_controls
from app.market.calendar import MarketCalendarService
from app.models import DailyWorkflowRun
from app.workflow.lease import LeaseService
from app.workflow.states import DailyWorkflowState, WorkflowRunStatus

logger = get_logger(__name__)


class RecoveryService:
    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.calendar = MarketCalendarService(self.settings)
        self.leases = LeaseService(session, self.settings)

    async def run(self, *, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        restored = await restore_trading_controls(self.session, trading_controls)
        reclaimed = await self.leases.reclaim_expired()
        actions: list[str] = []
        if restored:
            actions.append(f"restored_ops_state:{restored.get('state')}")
        emergency = trading_controls.snapshot().state.value == "emergency_stop"
        if emergency:
            actions.append("emergency_stop_preserved")

        open_runs = list(
            (
                await self.session.execute(
                    select(DailyWorkflowRun).where(
                        DailyWorkflowRun.status.in_(
                            [
                                WorkflowRunStatus.PENDING.value,
                                WorkflowRunStatus.RUNNING.value,
                                WorkflowRunStatus.PAUSED.value,
                            ]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        for run in open_runs:
            session_day = datetime.fromisoformat(run.session_date).date()
            today = now.astimezone(self.calendar.market_tz).date()
            if run.current_state == DailyWorkflowState.EMERGENCY_STOP.value:
                actions.append(f"keep_emergency:{run.session_date}")
                continue
            if session_day < today and run.status != WorkflowRunStatus.COMPLETED.value:
                # Stale prior day — mark failed for operator review (no late orders)
                run.status = WorkflowRunStatus.FAILED.value
                run.failed_at = now
                run.failure_reason = "stale_incomplete_run_after_session"
                run.current_state = DailyWorkflowState.FAILED.value
                actions.append(f"fail_stale:{run.session_date}")
                continue
            if run.current_state == DailyWorkflowState.POSTMARKET_REVIEW.value:
                actions.append(f"resume_postmarket_eligible:{run.session_date}")
            elif run.current_state in {
                DailyWorkflowState.PREMARKET_PREPARATION.value,
                DailyWorkflowState.PREMARKET_ANALYSIS.value,
            }:
                status = self.calendar.get_market_status(now)
                if status.phase in {"PREMARKET", "BEFORE_PREMARKET"}:
                    actions.append(f"resume_premarket_eligible:{run.session_date}")
                else:
                    actions.append(f"missed_premarket_use_no_trade_default:{run.session_date}")
                    meta = dict(run.metadata_json or {})
                    meta["recovery_note"] = "missed_premarket_after_open"
                    run.metadata_json = meta
            elif run.current_state == DailyWorkflowState.CLOSING_WINDOW.value:
                if self.calendar.get_market_status(now).phase in {"POSTMARKET", "AFTER_HOURS"}:
                    actions.append(f"missed_closing_no_orders:{run.session_date}")
                    meta = dict(run.metadata_json or {})
                    meta["recovery_note"] = "missed_closing_window"
                    run.metadata_json = meta

        await self.session.flush()
        logger.info("recovery_complete", reclaimed_leases=reclaimed, actions=actions)
        return {
            "reclaimed_leases": reclaimed,
            "actions": actions,
            "emergency_stop": emergency,
            "as_of": now.isoformat(),
        }
