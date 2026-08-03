"""APScheduler wiring — dispatches to DailyWorkflowService only (no LLM/broker)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.execution.safety_controls import trading_controls
from app.workflow.lease import LeaseError, LeaseService

logger = get_logger(__name__)

_scheduler: AsyncIOScheduler | None = None
_job_log: list[dict[str, Any]] = []


def get_scheduler() -> AsyncIOScheduler | None:
    return _scheduler


def upcoming_jobs() -> list[dict[str, Any]]:
    sched = _scheduler
    if sched is None:
        return list(_job_log)
    jobs = []
    for job in sched.get_jobs():
        jobs.append(
            {
                "id": job.id,
                "name": job.name,
                "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
            }
        )
    return jobs


def _scheduler_enabled(cfg: Settings) -> bool:
    # Phase 3 primary flag; legacy scheduler_enabled alone must not enable jobs.
    return bool(cfg.enable_scheduler)


async def _dispatch_due_jobs() -> None:
    """Poll due scheduled_jobs and invoke DailyWorkflowService methods."""
    from app.core.database import get_session_factory
    from app.models import ScheduledJobRecord
    from app.workflow.daily import DailyWorkflowError, DailyWorkflowService
    from sqlalchemy import select

    if trading_controls.snapshot().state.value == "emergency_stop":
        logger.info("scheduler_skip_emergency_stop")
        return

    settings = get_settings()
    factory = get_session_factory()
    now = datetime.now(UTC)
    async with factory() as session:
        leases = LeaseService(session, settings)
        try:
            await leases.acquire("scheduler:dispatch", "scheduler")
        except LeaseError:
            logger.info("scheduler_dispatch_lease_held")
            return
        try:
            svc = DailyWorkflowService(session, settings=settings, owner="scheduler")
            candidates = list(
                (
                    await session.execute(
                        select(ScheduledJobRecord)
                        .where(ScheduledJobRecord.status == "planned")
                        .order_by(ScheduledJobRecord.planned_at)
                        .limit(50)
                    )
                )
                .scalars()
                .all()
            )

            def _due(planned: datetime) -> bool:
                if planned.tzinfo is None:
                    planned = planned.replace(tzinfo=UTC)
                return planned <= now

            due = [j for j in candidates if _due(j.planned_at)][:20]
            for job in due:
                job_lease = f"job:{job.session_date}:{job.job_key}"
                try:
                    await leases.acquire(job_lease, "scheduler")
                except LeaseError:
                    continue
                try:
                    if job.status != "planned":
                        continue
                    job.status = "running"
                    job.started_at = now
                    await session.flush()
                    await _run_job_action(svc, job.job_key, job.session_date)
                    job.status = "completed"
                    job.completed_at = datetime.now(UTC)
                    entry = {
                        "job": job.job_key,
                        "session_date": job.session_date,
                        "status": "completed",
                        "at": job.completed_at.isoformat(),
                    }
                    _job_log.append(entry)
                    if len(_job_log) > 100:
                        del _job_log[:-100]
                    logger.info("scheduler_job_done", **entry)
                except DailyWorkflowError as exc:
                    job.status = "skipped"
                    job.error = str(exc)
                    logger.warning("scheduler_job_skipped", job=job.job_key, error=str(exc))
                except Exception as exc:  # noqa: BLE001
                    job.status = "failed"
                    job.error = str(exc)
                    logger.exception("scheduler_job_failed", job=job.job_key)
                finally:
                    try:
                        await leases.release(job_lease, "scheduler")
                    except LeaseError:
                        pass
            await session.commit()
        finally:
            try:
                await leases.release("scheduler:dispatch", "scheduler")
                await session.commit()
            except LeaseError:
                await session.commit()


async def _run_job_action(svc: Any, job_key: str, session_date: str) -> None:
    settings = get_settings()
    fake = not bool(settings.llm_api_key)
    if job_key == "premarket_preparation":
        await svc.prepare(session_date=session_date)
    elif job_key == "premarket_analysis":
        await svc.run_analysis(session_date=session_date, fake_llm=fake)
    elif job_key == "preopen_revalidation":
        await svc.revalidate(session_date=session_date, fake_llm=fake)
    elif job_key.startswith("intraday_eval"):
        await svc.evaluate_intraday(session_date=session_date, trigger="interval")
    elif job_key == "closing_window":
        await svc.start_closing(session_date=session_date)
    elif job_key == "postmarket_review":
        await svc.run_postmarket(session_date=session_date)
    else:
        logger.warning("unknown_scheduled_job", job_key=job_key)


def start_scheduler(settings: Settings | None = None) -> AsyncIOScheduler | None:
    global _scheduler
    cfg = settings or get_settings()
    if not _scheduler_enabled(cfg):
        logger.info("scheduler_disabled")
        return None
    if _scheduler is not None:
        return _scheduler

    sched = AsyncIOScheduler(timezone=cfg.market_timezone)
    # Dynamic session jobs are planned in DB; poll frequently and dispatch.
    sched.add_job(
        _dispatch_due_jobs,
        IntervalTrigger(seconds=max(30, cfg.workflow_heartbeat_seconds)),
        id="daily_workflow_dispatch",
        replace_existing=True,
        name="daily_workflow_dispatch",
    )
    sched.start()
    _scheduler = sched
    logger.info(
        "scheduler_started",
        enable_scheduler=cfg.enable_scheduler,
        enable_broker_orders=cfg.enable_broker_orders,
        enable_automated_execution=cfg.enable_automated_execution,
        jobs=[j["id"] for j in upcoming_jobs()],
    )
    return sched


async def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("scheduler_stopped")
