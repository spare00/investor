"""APScheduler wiring — session bootstrap + DailyWorkflowService dispatch + universe refresh + broker recon."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
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

# Bound a single due-job action so a wedged IBKR/LLM call cannot pin
# daily_workflow_dispatch forever (APScheduler max_instances=1).
# Local and cloud share the 8-minute cap.
_JOB_ACTION_TIMEOUT_SECONDS = 480
_CATCH_UP_TIMEOUT_SECONDS = 480
_UNIVERSE_REFRESH_TIMEOUT_SECONDS = 900
_BROKER_RECON_TIMEOUT_SECONDS = 120


def _scheduler_job_kind(job_key: str) -> str:
    key = job_key.split(":", 1)[-1]
    if key.startswith("intraday_eval"):
        return "intraday_eval"
    if key.startswith("postmarket_eval"):
        return "postmarket_eval"
    if key.startswith("premarket"):
        return "premarket"
    if key.startswith("universe"):
        return "universe"
    return key or "other"


async def _reap_stale_running_jobs(session: Any, settings: Settings, now: datetime) -> int:
    """Fail running rows older than the job timeout so a wedged eval cannot linger."""
    from sqlalchemy import select
    from app.models import ScheduledJobRecord

    timeout_s = float(settings.effective_job_action_timeout_seconds())
    cutoff = now - timedelta(seconds=timeout_s + 60)
    rows = list(
        (
            await session.execute(
                select(ScheduledJobRecord).where(
                    ScheduledJobRecord.status == "running",
                    ScheduledJobRecord.started_at.is_not(None),
                    ScheduledJobRecord.started_at < cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0
    for job in rows:
        started = job.started_at
        if started is not None and started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        job.status = "failed"
        job.error = f"stale_running_reaped:{int(timeout_s)}s"
        job.completed_at = (
            started + timedelta(seconds=int(timeout_s)) if started is not None else now
        )
        logger.error(
            "scheduler_stale_job_reaped",
            job=job.job_key,
            session_date=job.session_date,
            started_at=str(started),
        )
    await session.flush()
    return len(rows)


def _observe_scheduler_job(
    job: Any,
    *,
    timeout_s: float,
    timed_out: bool,
) -> None:
    """Record wall time vs the 8-minute cap so growing books are visible."""
    from app.core.metrics import (
        COMMITTEE_HEADROOM_RATIO,
        COMMITTEE_TIMEOUT_CAP_SECONDS,
        LAST_COMMITTEE_SECONDS,
        SCHEDULER_JOB_DURATION,
        SCHEDULER_JOB_TIMEOUTS,
    )

    kind = _scheduler_job_kind(str(getattr(job, "job_key", "") or ""))
    elapsed: float | None = None
    started = getattr(job, "started_at", None)
    if started is not None:
        start = started if started.tzinfo else started.replace(tzinfo=UTC)
        elapsed = max(0.0, (datetime.now(UTC) - start).total_seconds())
        SCHEDULER_JOB_DURATION.labels(kind=kind).observe(elapsed)
    if timed_out:
        SCHEDULER_JOB_TIMEOUTS.labels(kind=kind).inc()
    if kind == "intraday_eval":
        COMMITTEE_TIMEOUT_CAP_SECONDS.set(float(timeout_s))
        if elapsed is not None:
            LAST_COMMITTEE_SECONDS.set(elapsed)
            COMMITTEE_HEADROOM_RATIO.set(max(0.0, 1.0 - elapsed / max(1.0, float(timeout_s))))


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


# Premarket through after-hours only — skip BEFORE_PREMARKET / non-trading overnight.
_UNIVERSE_REFRESH_PHASES = frozenset(
    {
        "PREMARKET",
        "REGULAR",
        "FORCE_CLOSE_WINDOW",
        "CLOSING_WINDOW",
        "POSTMARKET",
        "AFTER_HOURS",
    }
)


def _is_operator_weekend(cfg: Settings, now: datetime | None = None) -> bool:
    """Saturday/Sunday in operator_timezone (default Australia/Brisbane)."""
    from app.universe.schedule import is_operator_weekend

    return is_operator_weekend(cfg, now)

def _universe_refresh_allowed_now(cfg: Settings, now: datetime | None = None) -> bool:
    """Whether a periodic Universe Manager poll should run at this clock time.

    Weekend-only (default) keeps the weekly LLM off weekday trading sessions.
    Session-only is a legacy fallback when weekend_only is false.
    """
    if bool(cfg.universe_refresh_weekend_only):
        return _is_operator_weekend(cfg, now)
    if not bool(cfg.universe_refresh_session_only):
        return True
    from app.market.calendar import MarketCalendarService
    from app.market.venues import enabled_venues

    for venue in enabled_venues(cfg):
        status = MarketCalendarService(cfg, venue=venue).get_market_status(now)
        if status.phase in _UNIVERSE_REFRESH_PHASES:
            return True
    return False


def _broker_recon_enabled(cfg: Settings) -> bool:
    """Periodic broker reconciliation when scheduler + broker connection/orders are on."""
    return _scheduler_enabled(cfg) and (
        bool(cfg.enable_broker_connection) or bool(cfg.enable_broker_orders)
    )


def _coalesce_due_jobs(due: list[Any]) -> list[Any]:
    """Keep only the latest overdue intraday_eval_* job per venue; mark older ones skipped."""
    from collections import defaultdict

    from app.market.venues import job_key_base, parse_scoped_job_key

    other: list[Any] = []
    by_venue: dict[Any, list[Any]] = defaultdict(list)
    for job in due:
        venue, _ = parse_scoped_job_key(job.job_key)
        if job_key_base(job.job_key).startswith("intraday_eval"):
            by_venue[venue].append(job)
        else:
            other.append(job)
    keep_intra: list[Any] = []
    for jobs in by_venue.values():
        if len(jobs) <= 1:
            keep_intra.extend(jobs)
            continue
        keep = max(jobs, key=lambda j: j.planned_at)
        for job in jobs:
            if job is keep:
                keep_intra.append(job)
                continue
            job.status = "skipped"
            job.error = "coalesced_stale_intraday"
            job.completed_at = datetime.now(UTC)
    out = other + keep_intra
    out.sort(key=lambda j: j.planned_at)
    return out


# Skip redundant prepare work between dispatch ticks (prepare itself is idempotent).
_PREPARE_CACHE: dict[str, datetime] = {}
_PREPARE_TTL_SECONDS = 600


async def _ensure_sessions_prepared(session: Any, settings: Settings) -> list[str]:
    """Idempotently prepare today + next trading day per enabled venue.

    Throttled per venue/day label so the ~60s dispatch poll does not re-acquire
    prepare leases when nothing changed. A new calendar day always misses cache.
    """
    from app.market.venues import enabled_venues
    from app.workflow.daily import DailyWorkflowService

    now = datetime.now(UTC)
    prepared: list[str] = []
    live_labels: set[str] = set()
    for venue in enabled_venues(settings):
        svc = DailyWorkflowService(session, settings=settings, owner="scheduler", venue=venue)
        today = datetime.now(UTC).astimezone(svc.calendar.market_tz).date()
        targets = sorted({today, svc.calendar.get_next_trading_day(today)})
        for day in targets:
            label = f"{venue.value}:{day.isoformat()}"
            live_labels.add(label)
            last = _PREPARE_CACHE.get(label)
            if last is not None and (now - last).total_seconds() < _PREPARE_TTL_SECONDS:
                continue
            result = await svc.prepare(session_date=day.isoformat())
            _PREPARE_CACHE[label] = now
            prepared.append(label)
            logger.info(
                "scheduler_session_prepared",
                venue=venue.value,
                session_date=day.isoformat(),
                note=result.get("note") or result.get("current_state"),
            )
    # Drop labels for days no longer in the rolling window.
    for stale in [k for k in _PREPARE_CACHE if k not in live_labels]:
        _PREPARE_CACHE.pop(stale, None)
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
            from app.market.venues import enabled_venues, parse_scoped_job_key

            reaped = await _reap_stale_running_jobs(session, settings, now)
            if reaped:
                await session.commit()

            try:
                await _ensure_sessions_prepared(session, settings)
                await session.commit()
            except DailyWorkflowError as exc:
                logger.warning("scheduler_prepare_skipped", error=str(exc))
                await session.commit()
            except Exception:  # noqa: BLE001
                logger.exception("scheduler_prepare_failed")
                await session.rollback()
                return

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

            due_all = [j for j in candidates if _due(j.planned_at)]
            due = _coalesce_due_jobs(due_all)[:20]
            services: dict[str, Any] = {}
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
                    await session.commit()
                    venue, _ = parse_scoped_job_key(job.job_key)
                    if venue.value not in services:
                        services[venue.value] = DailyWorkflowService(
                            session, settings=settings, owner="scheduler", venue=venue
                        )
                    try:
                        timeout_s = float(settings.effective_job_action_timeout_seconds())
                        outcome = await asyncio.wait_for(
                            _run_job_action(
                                services[venue.value], job.job_key, job.session_date
                            ),
                            timeout=timeout_s,
                        )
                    except TimeoutError:
                        from sqlalchemy import update

                        from app.market.venues import job_key_base

                        resume = job_key_base(job.job_key).startswith("postmarket_eval")
                        await session.execute(
                            update(ScheduledJobRecord)
                            .where(ScheduledJobRecord.id == job.id)
                            .values(
                                status="planned" if resume else "failed",
                                error=f"job_action_timeout:{int(timeout_s)}s",
                                planned_at=datetime.now(UTC) + timedelta(seconds=30),
                                started_at=None,
                                completed_at=None if resume else datetime.now(UTC),
                            )
                        )
                        _observe_scheduler_job(job, timeout_s=timeout_s, timed_out=True)
                        logger.error(
                            "scheduler_job_timeout",
                            job=job.job_key,
                            session_date=job.session_date,
                            timeout_s=timeout_s,
                            rescheduled=resume,
                        )
                        await session.commit()
                        continue
                    if isinstance(outcome, dict) and outcome.get("reschedule"):
                        delay = float(outcome.get("delay_s") or 0)
                        job.status = "planned"
                        job.planned_at = datetime.now(UTC) + timedelta(seconds=max(0.0, delay))
                        job.started_at = None
                        job.completed_at = None
                        job.error = None
                        logger.info(
                            "scheduler_job_rescheduled",
                            job=job.job_key,
                            session_date=job.session_date,
                            delay_s=delay,
                            remaining=outcome.get("remaining_decisions"),
                        )
                        await session.commit()
                        continue
                    job.status = "completed"
                    job.completed_at = datetime.now(UTC)
                    _observe_scheduler_job(job, timeout_s=timeout_s, timed_out=False)
                    entry = {
                        "job": job.job_key,
                        "session_date": job.session_date,
                        "venue": venue.value,
                        "status": "completed",
                        "at": job.completed_at.isoformat(),
                    }
                    _job_log.append(entry)
                    if len(_job_log) > 100:
                        del _job_log[:-100]
                    logger.info("scheduler_job_done", **entry)
                    await session.commit()
                except DailyWorkflowError as exc:
                    job.status = "skipped"
                    job.error = str(exc)
                    job.completed_at = datetime.now(UTC)
                    logger.warning("scheduler_job_skipped", job=job.job_key, error=str(exc))
                    await session.commit()
                except Exception as exc:  # noqa: BLE001
                    job.status = "failed"
                    job.error = str(exc)[:500]
                    job.completed_at = datetime.now(UTC)
                    try:
                        _observe_scheduler_job(
                            job,
                            timeout_s=float(settings.effective_job_action_timeout_seconds()),
                            timed_out=False,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    logger.exception("scheduler_job_failed", job=job.job_key)
                    await session.commit()
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

        # Catch-up (may run LLM) outside dispatch lease so long analysis does not
        # block due-job ticks. Venue analysis leases still serialize the work.
        from app.market.venues import enabled_venues

        try:
            await leases.acquire("scheduler:catch_up", "scheduler")
        except LeaseError:
            logger.info("scheduler_catch_up_lease_held")
            return
        try:
            fake = settings.scheduler_uses_fake_llm()
            for venue in enabled_venues(settings):
                svc = DailyWorkflowService(
                    session, settings=settings, owner="scheduler", venue=venue
                )
                try:
                    missed = await asyncio.wait_for(
                        svc.retry_missed_session_exits(now=now),
                        timeout=90,
                    )
                    if not missed.get("skipped", True):
                        logger.info(
                            "scheduler_missed_exits",
                            venue=venue.value,
                            orders_submitted=missed.get("orders_submitted"),
                            intent_ids=missed.get("intent_ids"),
                        )
                except TimeoutError:
                    logger.error(
                        "scheduler_missed_exits_timeout",
                        venue=venue.value,
                    )
                await session.commit()
            for venue in enabled_venues(settings):
                svc = DailyWorkflowService(
                    session, settings=settings, owner="scheduler", venue=venue
                )
                catch_timeout = float(settings.effective_job_action_timeout_seconds())
                try:
                    catch = await asyncio.wait_for(
                        svc.catch_up_to_intraday(fake_llm=fake, now=now),
                        timeout=catch_timeout,
                    )
                except TimeoutError:
                    logger.error(
                        "scheduler_catch_up_timeout",
                        venue=venue.value,
                        timeout_s=catch_timeout,
                    )
                    try:
                        from app.core.metrics import SCHEDULER_JOB_TIMEOUTS

                        SCHEDULER_JOB_TIMEOUTS.labels(kind="catch_up").inc()
                    except Exception:  # noqa: BLE001
                        pass
                    await session.rollback()
                    continue
                if not (catch.get("catch_up") or {}).get("skipped", True):
                    logger.info(
                        "scheduler_session_catch_up",
                        venue=venue.value,
                        **(catch.get("catch_up") or {}),
                    )
                await session.commit()
        except DailyWorkflowError as exc:
            logger.warning("scheduler_catch_up_skipped", error=str(exc))
            await session.commit()
        except Exception:  # noqa: BLE001
            logger.exception("scheduler_catch_up_failed")
            await session.rollback()
        finally:
            try:
                await leases.release("scheduler:catch_up", "scheduler")
                await session.commit()
            except LeaseError:
                await session.commit()


async def _run_job_action(svc: Any, job_key: str, session_date: str) -> dict[str, Any] | None:
    from app.market.venues import job_key_base

    settings = get_settings()
    fake = settings.scheduler_uses_fake_llm()
    action = job_key_base(job_key)
    if action == "premarket_preparation":
        await svc.prepare(session_date=session_date)
    elif action == "premarket_analysis":
        await svc.run_analysis(session_date=session_date, fake_llm=fake)
    elif action == "preopen_revalidation":
        await svc.revalidate(session_date=session_date, fake_llm=fake)
    elif action.startswith("intraday_eval"):
        await svc.evaluate_intraday(session_date=session_date, trigger="interval", fake_llm=fake)
    elif action == "closing_window":
        await svc.start_closing(session_date=session_date)
    elif action == "postmarket_review":
        await svc.run_postmarket(session_date=session_date)
    elif action == "postmarket_eval" or action.startswith("postmarket_eval"):
        result = await svc.run_postmarket_eval(session_date=session_date)
        eval_payload = result.get("eval") if isinstance(result, dict) else None
        return eval_payload if isinstance(eval_payload, dict) else None
    else:
        logger.warning("unknown_scheduled_job", job_key=job_key)
    return None


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
    if not _universe_refresh_allowed_now(settings):
        from app.market.calendar import MarketCalendarService

        phase = MarketCalendarService(settings).get_market_status().phase
        logger.info("universe_refresh_skip_off_session", phase=phase)
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
                result = await asyncio.wait_for(
                    svc.refresh(holdings=holdings),
                    timeout=float(_UNIVERSE_REFRESH_TIMEOUT_SECONDS),
                )
                replan: dict[str, Any] = {}
                try:
                    from app.market.venues import enabled_venues
                    from app.workflow.daily import DailyWorkflowService

                    replan = {}
                    for venue in enabled_venues(settings):
                        part = await DailyWorkflowService(
                            session, settings=settings, owner="scheduler", venue=venue
                        ).replan_intraday_jobs()
                        replan[venue.value] = part
                        await session.commit()
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
                    "replan": {
                        v: {
                            "purged": (part or {}).get("purged"),
                            "created": (part or {}).get("created"),
                        }
                        for v, part in (replan or {}).items()
                        if isinstance(part, dict)
                    }
                    if isinstance(replan, dict) and any(
                        isinstance(v, dict) for v in replan.values()
                    )
                    else None,
                    "replan_purged": replan.get("purged") if isinstance(replan, dict) else None,
                    "replan_created": replan.get("created") if isinstance(replan, dict) else None,
                }
                _job_log.append(entry)
                if len(_job_log) > 100:
                    del _job_log[:-100]
                logger.info("universe_refresh_done", **{k: v for k, v in entry.items() if v is not None})
            except TimeoutError:
                await session.rollback()
                logger.error(
                    "universe_refresh_timeout",
                    timeout_s=_UNIVERSE_REFRESH_TIMEOUT_SECONDS,
                )
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
                await _reap_stale_running_jobs(session, settings, datetime.now(UTC))
                await session.commit()

                async def _recon_once() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], Any]:
                    from app.execution.order_manager import OrderManager
                    from app.intraday.broker_updates import BrokerUpdateProcessor

                    # Heal local open orders missing at Gateway before comparing books.
                    try:
                        await OrderManager(session, settings=settings).sync_statuses_from_broker()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("broker_recon_order_sync_failed", error=str(exc)[:200])

                    recon_svc = ReconciliationService(session, settings=settings)
                    book = None
                    try:
                        book = await recon_svc.fetch_book()
                    except Exception as exc:  # noqa: BLE001
                        recon = await recon_svc.run("SCHEDULED")  # records BROKER_UNAVAILABLE
                        recon.setdefault("fetch_error", str(exc)[:200])
                    else:
                        recon = await recon_svc.run("SCHEDULED", book=book)
                    try:
                        from app.alerts.ops import emit_reconciliation_alert

                        await emit_reconciliation_alert(
                            session,
                            settings,
                            result=str(recon.get("result") or ""),
                            issues=list(recon.get("issues") or []),
                            sync_type="SCHEDULED",
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("broker_recon_alert_failed", error=str(exc)[:200])
                    poll: dict[str, Any] = {}
                    sync: dict[str, Any] = {}
                    book = recon.get("book") or book
                    if book is not None:
                        try:
                            poll = await BrokerUpdateProcessor(
                                session, settings=settings
                            ).poll_and_apply(remote_orders=book.orders)
                        except Exception as exc:  # noqa: BLE001
                            poll = {"error": str(exc)[:200]}
                        try:
                            sync = await PositionManager(session, settings=settings).sync_from_broker(
                                account=book.account,
                                positions=book.positions,
                            )
                        except Exception as exc:  # noqa: BLE001
                            sync = {"error": str(exc)[:200]}
                    return recon, poll, sync, book

                recon, poll, sync, book = await asyncio.wait_for(
                    _recon_once(),
                    timeout=float(_BROKER_RECON_TIMEOUT_SECONDS),
                )
                await session.commit()
                entry = {
                    "job": "broker_reconciliation",
                    "status": "completed",
                    "at": datetime.now(UTC).isoformat(),
                    "result": recon.get("result"),
                    "issues": len(recon.get("issues") or []),
                    "blocks_new_orders": recon.get("blocks_new_orders"),
                    "poll_updated": poll.get("updated"),
                    "poll_unchanged": poll.get("skipped_unchanged"),
                    "poll_error": poll.get("error"),
                    "sync_error": sync.get("error"),
                    "snapshot_written": sync.get("snapshot_written"),
                    "lifecycles_upserted": (sync.get("lifecycles") or {}).get("upserted"),
                    "lifecycles_closed": (sync.get("lifecycles") or {}).get("closed"),
                    "shared_book": book is not None,
                }
                _job_log.append(entry)
                if len(_job_log) > 100:
                    del _job_log[:-100]
                logger.info(
                    "broker_recon_done",
                    **{k: v for k, v in entry.items() if v is not None},
                )
            except TimeoutError:
                await session.rollback()
                logger.error(
                    "broker_recon_timeout",
                    timeout_s=_BROKER_RECON_TIMEOUT_SECONDS,
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
