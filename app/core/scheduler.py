"""APScheduler wiring for market workflows."""

from __future__ import annotations

from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

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


async def _noop_job(name: str) -> None:
    """
    Placeholder scheduled hook.

    Real DB-backed workflow runs are triggered via API or a worker with a session
    factory. Scheduling here records intent for /status visibility.
    """
    entry = {"job": name, "status": "tick"}
    _job_log.append(entry)
    if len(_job_log) > 100:
        del _job_log[:-100]
    logger.info("scheduler_tick", job=name)


def start_scheduler(settings: Settings | None = None) -> AsyncIOScheduler | None:
    global _scheduler
    cfg = settings or get_settings()
    if not cfg.scheduler_enabled:
        logger.info("scheduler_disabled")
        return None
    if _scheduler is not None:
        return _scheduler

    sched = AsyncIOScheduler(timezone=cfg.premarket_cron_tz)
    sched.add_job(
        _noop_job,
        CronTrigger(
            hour=cfg.premarket_cron_hour,
            minute=cfg.premarket_cron_minute,
            timezone=cfg.premarket_cron_tz,
        ),
        args=["premarket"],
        id="premarket",
        replace_existing=True,
        name="premarket_workflow",
    )
    sched.add_job(
        _noop_job,
        CronTrigger(hour=10, minute=0, timezone=cfg.premarket_cron_tz),
        args=["intraday"],
        id="intraday_sample",
        replace_existing=True,
        name="intraday_sample_tick",
    )
    sched.add_job(
        _noop_job,
        CronTrigger(hour=16, minute=5, timezone=cfg.premarket_cron_tz),
        args=["postmarket"],
        id="postmarket",
        replace_existing=True,
        name="postmarket_workflow",
    )
    sched.start()
    _scheduler = sched
    logger.info("scheduler_started", jobs=[j["id"] for j in upcoming_jobs()])
    return sched


async def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("scheduler_stopped")
