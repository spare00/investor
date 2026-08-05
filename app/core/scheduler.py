"""APScheduler wiring — session bootstrap + DailyWorkflowService dispatch + universe refresh + broker recon."""

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


def _universe_refresh_enabled(cfg: Settings) -> bool:
    """Periodic Universe Manager runs only under the main scheduler gate."""
    return (
        _scheduler_enabled(cfg)
        and bool(cfg.universe_manager_enabled)
        and (cfg.universe_mode or "dynamic").lower() == "dynamic"
    )


def _broker_recon_enabled(cfg: Settings) -> bool:
    """Periodic broker reconciliation when scheduler + broker connection/orders are on."""
    return _scheduler_enabled(cfg) and (
        bool(cfg.enable_broker_connection) or bool(cfg.enable_broker_orders)
    )


def _coalesce_due_jobs(due: list[Any]) -> list[Any]:
    """Keep only the latest overdue intraday_eval_* job; mark older ones skipped."""
    other = [j for j in due if not str(j.job_key).startswith("intraday_eval")]
    intra = [j for j in due if str(j.job_key).startswith("intraday_eval")]
    if len(intra) <= 1:
        return due
    keep = max(intra, key=lambda j: j.planned_at)
    for job in intra:
        if job is keep:
            continue
        job.status = "skipped"
        job.error = "coalesced_stale_intraday"
        job.completed_at = datetime.now(UTC)
    out = other + [keep]
    out.sort(key=lambda j: j.planned_at)
    return out


async def _ensure_sessions_prepared(svc: Any, settings: Settings) -> list[str]:
    """Idempotently prepare today + next trading day so planned jobs exist unattended."""
    from app.market.calendar import MarketCalendarService

    cal = MarketCalendarService(settings)
    today = datetime.now(UTC).astimezone(cal.market_tz).date()
    targets = sorted({today, cal.get_next_trading_day(today)})
    prepared: list[str] = []
    for day in targets:
        result = await svc.prepare(session_date=day.isoformat())
        prepared.append(day.isoformat())
        logger.info(
            "scheduler_session_prepared",
            session_date=day.isoformat(),
            note=result.get("note") or result.get("current_state"),
        )
    return prepared


async def _dispatch_due_jobs() -> None:
    """Bootstrap session plans, then poll due scheduled_jobs and run DailyWorkflowService."""
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
            try:
                await _ensure_sessions_prepared(svc, settings)
                await session.commit()
            except DailyWorkflowError as exc:
                logger.warning("scheduler_prepare_skipped", error=str(exc))
                await session.commit()
            except Exception:  # noqa: BLE001
                logger.exception("scheduler_prepare_failed")
                await session.rollback()
                return

            # Near open / already open with incomplete prep → catch up (throttled).
            try:
                fake = not bool(settings.llm_api_key)
                catch = await svc.catch_up_to_intraday(fake_llm=fake, now=now)
                if not (catch.get("catch_up") or {}).get("skipped", True):
                    logger.info("scheduler_session_catch_up", **(catch.get("catch_up") or {}))
                await session.commit()
            except DailyWorkflowError as exc:
                logger.warning("scheduler_catch_up_skipped", error=str(exc))
                await session.commit()
            except Exception:  # noqa: BLE001
                logger.exception("scheduler_catch_up_failed")
                await session.rollback()

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

            due = _coalesce_due_jobs([j for j in candidates if _due(j.planned_at)][:20])
            for job in due:
                if job.status != "planned":
                    continue
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
        await svc.evaluate_intraday(session_date=session_date, trigger="interval", fake_llm=fake)
    elif job_key == "closing_window":
        await svc.start_closing(session_date=session_date)
    elif job_key == "postmarket_review":
        await svc.run_postmarket(session_date=session_date)
    else:
        logger.warning("unknown_scheduled_job", job_key=job_key)


async def _refresh_universe() -> None:
    """Periodic watchlist/focus refresh (leased; skips when emergency-stopped)."""
    from sqlalchemy import select

    from app.core.database import get_session_factory
    from app.models import Position
    from app.universe.service import UniverseService

    if trading_controls.snapshot().state.value == "emergency_stop":
        logger.info("universe_refresh_skip_emergency_stop")
        return

    settings = get_settings()
    if not _universe_refresh_enabled(settings):
        return

    factory = get_session_factory()
    async with factory() as session:
        leases = LeaseService(session, settings)
        try:
            await leases.acquire("scheduler:universe_refresh", "scheduler")
        except LeaseError:
            logger.info("universe_refresh_lease_held")
            return
        try:
            holdings = [
                p.symbol for p in (await session.execute(select(Position))).scalars().all()
            ]
            svc = UniverseService(session, settings=settings)
            try:
                result = await svc.refresh(holdings=holdings)
                replan: dict[str, Any] = {}
                try:
                    from app.workflow.daily import DailyWorkflowService

                    replan = await DailyWorkflowService(
                        session, settings=settings, owner="scheduler"
                    ).replan_intraday_jobs()
                except Exception:  # noqa: BLE001
                    logger.exception("universe_refresh_replan_failed")
                    replan = {"skipped": True, "reason": "replan_failed"}
                await session.commit()
                entry = {
                    "job": "universe_refresh",
                    "status": "completed" if not result.get("skipped") else "skipped",
                    "at": datetime.now(UTC).isoformat(),
                    "proposals": result.get("proposals"),
                    "reason": result.get("reason"),
                    "replan_purged": replan.get("purged"),
                    "replan_created": replan.get("created"),
                }
                _job_log.append(entry)
                if len(_job_log) > 100:
                    del _job_log[:-100]
                logger.info("universe_refresh_done", **{k: v for k, v in entry.items() if v is not None})
            except Exception:  # noqa: BLE001
                await session.rollback()
                logger.exception("universe_refresh_failed")
        finally:
            try:
                await leases.release("scheduler:universe_refresh", "scheduler")
                await session.commit()
            except LeaseError:
                await session.commit()


async def _reconcile_broker() -> None:
    """Periodic broker ↔ local reconciliation (+ soft position sync)."""
    from app.core.database import get_session_factory
    from app.execution.position_manager import PositionManager
    from app.execution.reconciliation import ReconciliationService

    if trading_controls.snapshot().state.value == "emergency_stop":
        logger.info("broker_recon_skip_emergency_stop")
        return

    settings = get_settings()
    if not _broker_recon_enabled(settings):
        return

    factory = get_session_factory()
    async with factory() as session:
        leases = LeaseService(session, settings)
        try:
            await leases.acquire("scheduler:broker_recon", "scheduler")
        except LeaseError:
            logger.info("broker_recon_lease_held")
            return
        try:
            try:
                recon = await ReconciliationService(session, settings=settings).run("SCHEDULED")
                sync: dict[str, Any] = {}
                try:
                    sync = await PositionManager(session, settings=settings).sync_from_broker()
                except Exception as exc:  # noqa: BLE001
                    sync = {"error": str(exc)[:200]}
                await session.commit()
                entry = {
                    "job": "broker_reconciliation",
                    "status": "completed",
                    "at": datetime.now(UTC).isoformat(),
                    "result": recon.get("result"),
                    "issues": len(recon.get("issues") or []),
                    "blocks_new_orders": recon.get("blocks_new_orders"),
                    "sync_error": sync.get("error"),
                }
                _job_log.append(entry)
                if len(_job_log) > 100:
                    del _job_log[:-100]
                logger.info(
                    "broker_recon_done",
                    **{k: v for k, v in entry.items() if v is not None},
                )
            except Exception:  # noqa: BLE001
                await session.rollback()
                logger.exception("broker_recon_failed")
        finally:
            try:
                await leases.release("scheduler:broker_recon", "scheduler")
                await session.commit()
            except LeaseError:
                await session.commit()


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
    if _universe_refresh_enabled(cfg):
        interval = max(120, int(cfg.universe_refresh_seconds))
        sched.add_job(
            _refresh_universe,
            IntervalTrigger(seconds=interval),
            id="universe_refresh",
            replace_existing=True,
            name="universe_refresh",
        )
    if _broker_recon_enabled(cfg):
        recon_interval = max(30, int(cfg.broker_reconciliation_interval_seconds))
        sched.add_job(
            _reconcile_broker,
            IntervalTrigger(seconds=recon_interval),
            id="broker_reconciliation",
            replace_existing=True,
            name="broker_reconciliation",
        )
    sched.start()
    _scheduler = sched
    logger.info(
        "scheduler_started",
        enable_scheduler=cfg.enable_scheduler,
        enable_broker_orders=cfg.enable_broker_orders,
        enable_automated_execution=cfg.enable_automated_execution,
        universe_refresh=_universe_refresh_enabled(cfg),
        broker_reconciliation=_broker_recon_enabled(cfg),
        jobs=[j["id"] for j in upcoming_jobs()],
    )
    return sched


async def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("scheduler_stopped")
