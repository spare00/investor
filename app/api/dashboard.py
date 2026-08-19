"""Dashboard and audit read APIs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.activity import (
    AGENT_ORDER,
    AGENT_SHORT,
    classify_agent_lamp,
    snapshot_agent_activity,
)
from app.core.config import get_settings
from app.core.database import get_db_session
from app.core.metrics import (
    FOCUS_SYMBOLS,
    WATCHLIST_SYMBOLS,
    metrics_payload,
)
from app.ops.committee_watch import build_committee_watch, job_duration_seconds
from app.core.scheduler import upcoming_jobs
from app.core.timeutils import dual_timezone_labels, utc_now
from app.execution.order_manager import OrderManager
from app.execution.safety_controls import trading_controls
from app.market.calendar import MarketCalendarService
from app.models import (
    AgentReport,
    AgentRun,
    AlertRecordModel,
    BrokerReconciliationRun,
    CIODecisionRecord,
    ClosingReview,
    IntradayEvent,
    IntradayRecoveryRun,
    NewsItem,
    Order,
    OrderIntent,
    OvernightReview,
    PortfolioSnapshot,
    Position,
    PositionLifecycle,
    PostmarketSettlement,
    ScheduledJobRecord,
    SystemEvent,
)
from app.services.briefing import BriefingService
from app.services.llm_budget import snapshot_llm_budget
from app.universe.reeval import effective_max_intraday_reanalyses
from app.workflow.daily import DailyWorkflowService

router = APIRouter(tags=["dashboard"])


def _parse_session_job_key(job_key: str) -> dict[str, Any]:
    raw = str(job_key or "")
    venue, _, rest = raw.partition(":")
    if not rest:
        rest = raw
        venue = ""
    if rest.startswith("intraday_eval_"):
        try:
            plan_index = int(rest.rsplit("_", 1)[-1])
        except ValueError:
            plan_index = None
        return {"venue": venue, "job_type": "intraday_eval", "plan_index": plan_index}
    if rest == "postmarket_eval" or rest.startswith("postmarket_eval_"):
        plan_index = None
        if rest.startswith("postmarket_eval_"):
            try:
                plan_index = int(rest.rsplit("_", 1)[-1])
            except ValueError:
                plan_index = None
        return {"venue": venue, "job_type": "postmarket_eval", "plan_index": plan_index}
    return {"venue": venue, "job_type": rest, "plan_index": None}


def _session_job_display_name(
    job_type: str, *, intraday_seq: int | None, plan_index: int | None = None
) -> str:
    if job_type == "intraday_eval" and intraday_seq is not None:
        return f"Intraday eval #{intraday_seq}"
    if job_type == "postmarket_eval":
        if plan_index is not None:
            return f"Postmarket eval #{plan_index + 1}"
        return "Postmarket eval"
    labels = {
        "premarket_analysis": "Premarket analysis",
        "closing_window": "Closing window",
        "postmarket_review": "Postmarket review",
        "universe_refresh": "Universe refresh",
        "force_close": "Force close",
    }
    return labels.get(job_type, job_type.replace("_", " "))


def enrich_session_jobs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add book-day sequence numbers and human labels (independent of plan_index suffix)."""
    by_venue: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        parsed = _parse_session_job_key(str(row.get("job_key") or ""))
        venue = str(row.get("venue") or parsed["venue"] or "")
        by_venue.setdefault(venue, []).append({**row, "venue": venue})

    enriched: list[dict[str, Any]] = []
    for _venue, group in by_venue.items():
        group.sort(key=lambda r: str(r.get("planned_at") or ""))
        intra_n = 0
        for session_seq, row in enumerate(group, start=1):
            parsed = _parse_session_job_key(str(row.get("job_key") or ""))
            job_type = str(parsed["job_type"])
            intra_seq: int | None = None
            if job_type == "intraday_eval":
                intra_n += 1
                intra_seq = intra_n
            enriched.append(
                {
                    **row,
                    "session_seq": session_seq,
                    "intraday_seq": intra_seq,
                    "job_type": job_type,
                    "plan_index": parsed["plan_index"],
                    "display_name": _session_job_display_name(
                        job_type, intraday_seq=intra_seq, plan_index=parsed["plan_index"]
                    ),
                    "duration_s": job_duration_seconds(row),
                }
            )
    return enriched


def _enriched_next_jobs() -> list[dict[str, Any]]:
    """APScheduler runtime pollers only — session plan lives in session_jobs."""
    rows: list[dict[str, Any]] = []
    for job in upcoming_jobs():
        nrt = job.get("next_run_time")
        labels: dict[str, Any] = {"utc": None, "us_eastern": None, "brisbane": None}
        if nrt:
            try:
                labels = dual_timezone_labels(datetime.fromisoformat(str(nrt)))
            except ValueError:
                pass
        rows.append(
            {
                **job,
                "kind": "runtime",
                "display": labels,
                "next_run_et": labels.get("us_eastern"),
                "next_run_bne": labels.get("brisbane"),
            }
        )

    return rows


@router.get("/metrics")
async def prometheus_metrics() -> Response:
    body, content_type = metrics_payload()
    return Response(content=body, media_type=content_type)


@router.get("/decisions")
async def list_decisions(
    limit: int = 20,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    result = await session.execute(
        select(CIODecisionRecord).order_by(desc(CIODecisionRecord.decision_timestamp)).limit(limit)
    )
    rows = list(result.scalars().all())
    return {
        "decisions": [
            {
                "decision_id": str(r.decision_id),
                "timestamp": r.decision_timestamp.isoformat(),
                "display": dual_timezone_labels(r.decision_timestamp),
                "market_regime": r.market_regime,
                "portfolio_action": r.portfolio_action,
                "risk_approval": r.risk_approval,
                "reason_not_to_trade": r.reason_not_to_trade,
                "payload": r.payload,
            }
            for r in rows
        ]
    }


@router.get("/dashboard/briefing")
async def dashboard_briefing(
    session_date: str | None = None,
    venue: str | None = None,
    raw: bool = False,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Readable daily report of agent materials that fed the CIO."""
    from app.market.session_ops import resolve_active_session_venue
    from app.market.venues import run_calendar_name

    settings = get_settings()
    active = resolve_active_session_venue(settings)
    book = (venue or (active.value if active else None) or "US").upper()
    calendar = run_calendar_name(book, settings)
    payload = await BriefingService(session).build(
        session_date=session_date or None,
        include_raw=bool(raw),
        calendar_name=calendar,
    )
    payload["venue"] = book
    payload["calendar_name"] = calendar
    return payload


@router.get("/agents/runs")
async def list_agent_runs(
    limit: int = 30,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    result = await session.execute(
        select(AgentRun).order_by(desc(AgentRun.started_at)).limit(limit)
    )
    runs = list(result.scalars().all())
    out = []
    for run in runs:
        reports = await session.execute(
            select(AgentReport).where(AgentReport.agent_run_id == run.id)
        )
        report_rows = list(reports.scalars().all())
        out.append(
            {
                "id": str(run.id),
                "workflow_id": str(run.workflow_id),
                "agent_name": run.agent_name,
                "status": run.status,
                "started_at": run.started_at.isoformat(),
                "model_name": run.model_name,
                "prompt_version": run.prompt_version,
                "reports": [
                    {
                        "report_type": r.report_type,
                        "data_quality_score": r.data_quality_score,
                        "payload": r.payload,
                    }
                    for r in report_rows
                ],
            }
        )
    return {"runs": out}


@router.get("/events")
async def list_events(
    limit: int = 50,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    result = await session.execute(
        select(SystemEvent).order_by(desc(SystemEvent.created_at)).limit(limit)
    )
    rows = list(result.scalars().all())
    return {
        "events": [
            {
                "id": str(e.id),
                "level": e.level,
                "event_type": e.event_type,
                "message": e.message,
                "context": e.context,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in rows
        ]
    }


# Throttle dashboard broker order sync so it doesn't compete with scheduled recon.
_LAST_DASHBOARD_ORDER_SYNC: datetime | None = None
_DASHBOARD_ORDER_SYNC_MIN_SECONDS = 90


@router.get("/dashboard/summary")
async def dashboard_summary(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    """Single payload for the ops dashboard."""
    global _LAST_DASHBOARD_ORDER_SYNC
    now = utc_now()
    controls = trading_controls.snapshot()

    # Prefer scheduled recon for broker truth; only refresh here when stale.
    sync_orders = False
    if _LAST_DASHBOARD_ORDER_SYNC is None:
        sync_orders = True
    else:
        age = (now - _LAST_DASHBOARD_ORDER_SYNC).total_seconds()
        sync_orders = age >= _DASHBOARD_ORDER_SYNC_MIN_SECONDS
    if sync_orders:
        try:
            await OrderManager(session).sync_statuses_from_broker()
            _LAST_DASHBOARD_ORDER_SYNC = now
        except Exception:  # noqa: BLE001 — dashboard should still render
            pass

    snap = (
        await session.execute(
            select(PortfolioSnapshot).order_by(desc(PortfolioSnapshot.as_of)).limit(1)
        )
    ).scalar_one_or_none()

    positions = list((await session.execute(select(Position))).scalars().all())
    open_orders = list(
        (
            await session.execute(
                select(Order).where(
                    Order.status.in_(["new", "accepted", "partially_filled", "pending_submit"])
                )
            )
        )
        .scalars()
        .all()
    )

    latest_decision = (
        await session.execute(
            select(CIODecisionRecord).order_by(desc(CIODecisionRecord.decision_timestamp)).limit(1)
        )
    ).scalar_one_or_none()

    # Latest report per agent
    agent_latest: dict[str, Any] = {}
    runs = list(
        (
            await session.execute(select(AgentRun).order_by(desc(AgentRun.started_at)).limit(48))
        )
        .scalars()
        .all()
    )
    live_activity = snapshot_agent_activity()
    for run in runs:
        if run.agent_name in agent_latest:
            continue
        report = (
            await session.execute(
                select(AgentReport).where(AgentReport.agent_run_id == run.id).limit(1)
            )
        ).scalar_one_or_none()
        live = live_activity.get(run.agent_name)
        lamp = classify_agent_lamp(
            live=live,
            last_run_status=run.status,
            last_started_at=run.started_at,
            now=now,
        )
        agent_latest[run.agent_name] = {
            "agent_name": run.agent_name,
            "short_name": AGENT_SHORT.get(run.agent_name, run.agent_name[:4].upper()),
            "model_name": run.model_name,
            "status": run.status,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "payload": report.payload if report else None,
            "data_quality_score": report.data_quality_score if report else None,
            "lamp": lamp["lamp"],
            "lamp_label": lamp["label"],
            "lamp_detail": lamp["detail"],
            "live": lamp["live"],
        }

    # Agents with live activity but no persisted run yet
    for name in AGENT_ORDER:
        if name in agent_latest:
            continue
        live = live_activity.get(name)
        lamp = classify_agent_lamp(
            live=live,
            last_run_status=None,
            last_started_at=None,
            now=now,
        )
        agent_latest[name] = {
            "agent_name": name,
            "short_name": AGENT_SHORT.get(name, name[:4].upper()),
            "model_name": None,
            "status": (live or {}).get("state"),
            "started_at": (live or {}).get("started_at"),
            "finished_at": (live or {}).get("finished_at"),
            "payload": None,
            "data_quality_score": None,
            "lamp": lamp["lamp"],
            "lamp_label": lamp["label"],
            "lamp_detail": lamp["detail"],
            "live": lamp["live"],
        }

    agent_lamps = [
        {
            "agent_name": name,
            "short_name": AGENT_SHORT.get(name, name[:4].upper()),
            "lamp": (agent_latest.get(name) or {}).get("lamp", "silent"),
            "lamp_label": (agent_latest.get(name) or {}).get("lamp_label", "inactive"),
            "lamp_detail": (agent_latest.get(name) or {}).get("lamp_detail", ""),
            "live": bool((agent_latest.get(name) or {}).get("live")),
        }
        for name in AGENT_ORDER
    ]

    news = list(
        (
            await session.execute(
                select(NewsItem)
                .where(NewsItem.is_duplicate.is_(False))
                .order_by(desc(NewsItem.published_at))
                .limit(8)
            )
        )
        .scalars()
        .all()
    )

    errors = list(
        (
            await session.execute(
                select(SystemEvent)
                .where(SystemEvent.level.in_(["error", "warning"]))
                .order_by(desc(SystemEvent.created_at))
                .limit(10)
            )
        )
        .scalars()
        .all()
    )

    settings = get_settings()
    from app.market.session_ops import active_session_summary
    from app.market.venues import enabled_venues, resolve_venue

    venue_sessions = {
        v.value: MarketCalendarService(settings, venue=v).get_market_status(now).to_dict()
        for v in enabled_venues(settings)
    }
    primary = resolve_venue(settings).value
    ops_target = active_session_summary(settings, now=now)
    us_session = venue_sessions.get("US") or MarketCalendarService(
        settings, venue="US"
    ).get_market_status(now).to_dict()
    au_session = venue_sessions.get("AU")
    workflow_summary: dict[str, Any] | None = None
    workflows_by_venue: dict[str, Any] = {}
    session_jobs: list[dict[str, Any]] = []
    active_ops_venue = str(ops_target.get("active_ops_venue") or primary)
    try:
        # Dual-book: load each venue's current session (US/AU dates often differ).
        for venue in enabled_venues(settings):
            daily = DailyWorkflowService(session, settings=settings, venue=venue)
            run = await daily.get_current()
            if run is None:
                continue
            meta = dict(run.metadata_json or {})
            summary = {
                "session_date": run.session_date,
                "state": run.current_state,
                "status": run.status,
                "venue": venue.value,
                "calendar_name": run.calendar_name,
                "intraday_reanalysis_count": int(run.intraday_reanalysis_count or 0),
                "max_intraday_reanalyses": int(
                    effective_max_intraday_reanalyses(settings)
                ),
                "last_intraday_eval_at": meta.get("last_intraday_eval_at"),
                "last_intraday_result": meta.get("last_intraday_result"),
                "last_force_close": meta.get("last_force_close"),
                "last_monitor": meta.get("last_monitor"),
                "last_news_ingest": meta.get("last_news_ingest"),
                "postmarket_review": meta.get("postmarket_review"),
                "postmarket_eval": meta.get("postmarket_eval"),
            }
            workflows_by_venue[venue.value] = summary
            jrows = list(
                (
                    await session.execute(
                        select(ScheduledJobRecord)
                        .where(ScheduledJobRecord.session_date == run.session_date)
                        .order_by(ScheduledJobRecord.planned_at)
                        .limit(300)
                    )
                )
                .scalars()
                .all()
            )
            for j in jrows:
                if not str(j.job_key).startswith(f"{venue.value}:"):
                    continue
                jmeta = j.metadata_json if isinstance(j.metadata_json, dict) else {}
                planned_iso = j.planned_at.isoformat() if j.planned_at else None
                labels = (
                    dual_timezone_labels(j.planned_at)
                    if j.planned_at is not None
                    else {"utc": None, "us_eastern": None, "brisbane": None}
                )
                session_jobs.append(
                    {
                        "job_key": j.job_key,
                        "session_date": j.session_date,
                        "planned_at": planned_iso,
                        "planned_at_et": labels.get("us_eastern"),
                        "planned_at_bne": labels.get("brisbane"),
                        "started_at": j.started_at.isoformat() if j.started_at else None,
                        "completed_at": j.completed_at.isoformat() if j.completed_at else None,
                        "status": j.status,
                        "error": j.error,
                        "interval_minutes": jmeta.get("interval_minutes"),
                        "venue": venue.value,
                    }
                )

        # Cadence strip follows the active ops venue (AU during ASX RTH).
        workflow_summary = workflows_by_venue.get(active_ops_venue) or workflows_by_venue.get(
            primary
        )
        if workflow_summary is None and workflows_by_venue:
            workflow_summary = next(iter(workflows_by_venue.values()))

        def _job_sort_key(row: dict[str, Any]) -> tuple[int, str]:
            # Active venue first, then chronological.
            venue_rank = 0 if row.get("venue") == active_ops_venue else 1
            return (venue_rank, str(row.get("planned_at") or ""))

        session_jobs = enrich_session_jobs(session_jobs)
        session_jobs.sort(key=_job_sort_key)
    except Exception:  # noqa: BLE001 — dashboard should still render
        workflow_summary = None
        workflows_by_venue = {}
        session_jobs = []

    universe_summary: dict[str, Any] | None = None
    try:
        from app.universe.service import UniverseService

        universe_summary = await UniverseService(session, settings=settings).snapshot()
    except Exception:  # noqa: BLE001
        universe_summary = None

    watch_n = 0
    focus_n = 0
    if isinstance(universe_summary, dict):
        watch_n = len(universe_summary.get("watchlist") or [])
        focus = universe_summary.get("focus") or {}
        if isinstance(focus, dict):
            focus_n = len(focus.get("symbols") or [])
        try:
            WATCHLIST_SYMBOLS.set(watch_n)
            FOCUS_SYMBOLS.set(focus_n)
        except Exception:  # noqa: BLE001
            pass
    committee_watch = build_committee_watch(
        session_jobs,
        timeout_cap_seconds=settings.effective_job_action_timeout_seconds(),
        watchlist_symbols=watch_n,
        focus_symbols=focus_n,
        allowlist_symbols=len(settings.trade_allowlist or []),
        llm_is_local=settings.llm_is_local(),
        now=now,
    )

    from app.execution.firm_execution import paper_auto_submit_allowed

    force_close_ops = {
        "auto_execute_force_close": bool(settings.auto_execute_force_close),
        "effective_auto_execute_force_close": bool(settings.effective_auto_execute_force_close()),
        "paper_auto_submit_allowed": paper_auto_submit_allowed(settings),
        "armed": bool(settings.effective_auto_execute_force_close())
        and paper_auto_submit_allowed(settings),
        "intraday_mode": settings.intraday_operation_mode,
    }

    hard_stop_ops = {
        "auto_execute_hard_stops": bool(settings.auto_execute_hard_stops),
        "effective_auto_execute_hard_stops": bool(settings.effective_auto_execute_hard_stops()),
        "enable_intraday_monitoring": bool(settings.enable_intraday_monitoring),
        "paper_auto_submit_allowed": paper_auto_submit_allowed(settings),
        "armed": bool(settings.effective_auto_execute_hard_stops())
        and paper_auto_submit_allowed(settings),
        "intraday_mode": settings.intraday_operation_mode,
    }

    monitor_positions: list[dict[str, Any]] = []
    try:
        lc_rows = list(
            (
                await session.execute(
                    select(PositionLifecycle)
                    .where(
                        PositionLifecycle.status.in_(
                            ["OPEN", "ADDING", "REDUCING", "PENDING_CLOSE", "PENDING_OPEN"]
                        )
                    )
                    .order_by(PositionLifecycle.updated_at.desc())
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
        for lc in lc_rows:
            monitor_positions.append(
                {
                    "id": str(lc.id),
                    "symbol": lc.symbol,
                    "status": lc.status,
                    "quantity": lc.quantity,
                    "current_price": lc.current_price,
                    "stop_price": lc.stop_price,
                    "take_profit_price": lc.take_profit_price,
                    "verdict": lc.last_monitor_verdict,
                    "unrealized_pl": lc.unrealized_pl,
                }
            )
    except Exception:  # noqa: BLE001
        monitor_positions = []

    pending_events: list[dict[str, Any]] = []
    try:
        ev_rows = list(
            (
                await session.execute(
                    select(IntradayEvent)
                    .where(IntradayEvent.status == "NEW")
                    .order_by(desc(IntradayEvent.detected_at))
                    .limit(12)
                )
            )
            .scalars()
            .all()
        )
        for ev in ev_rows:
            pending_events.append(
                {
                    "id": str(ev.id),
                    "event_type": ev.event_type,
                    "importance": ev.importance,
                    "symbols": ev.symbols or [],
                    "status": ev.status,
                    "requires_analysis": bool(ev.requires_analysis),
                    "detected_at": ev.detected_at.isoformat() if ev.detected_at else None,
                }
            )
    except Exception:  # noqa: BLE001
        pending_events = []

    latest_closing: dict[str, Any] | None = None
    try:
        crow = (
            await session.execute(select(ClosingReview).order_by(desc(ClosingReview.created_at)).limit(1))
        ).scalar_one_or_none()
        if crow is not None:
            latest_closing = {
                "id": str(crow.id),
                "policy": crow.policy,
                "created_at": crow.created_at.isoformat() if crow.created_at else None,
                "intent_drafts": crow.intent_drafts or [],
                "notes": crow.notes or [],
                "plans": (crow.payload or {}).get("plans") if isinstance(crow.payload, dict) else [],
            }
    except Exception:  # noqa: BLE001
        latest_closing = None

    latest_settlement: dict[str, Any] | None = None
    try:
        srow = (
            await session.execute(
                select(PostmarketSettlement).order_by(desc(PostmarketSettlement.created_at)).limit(1)
            )
        ).scalar_one_or_none()
        if srow is not None:
            latest_settlement = {
                "id": str(srow.id),
                "session_date": srow.session_date,
                "reconciliation_result": srow.reconciliation_result,
                "order_count": srow.order_count,
                "execution_count": srow.execution_count,
                "overnight_positions": srow.overnight_positions or [],
                "pnl_summary": srow.pnl_summary or [],
                "created_at": srow.created_at.isoformat() if srow.created_at else None,
            }
    except Exception:  # noqa: BLE001
        latest_settlement = None

    latest_reconciliation: dict[str, Any] | None = None
    try:
        rrow = (
            await session.execute(
                select(BrokerReconciliationRun)
                .order_by(desc(BrokerReconciliationRun.created_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        if rrow is not None:
            latest_reconciliation = {
                "id": str(rrow.id),
                "sync_type": rrow.sync_type,
                "result": rrow.result,
                "issues": rrow.issues or [],
                "payload": rrow.payload or {},
                "created_at": rrow.created_at.isoformat() if rrow.created_at else None,
            }
    except Exception:  # noqa: BLE001
        latest_reconciliation = None

    latest_recovery: dict[str, Any] | None = None
    try:
        rec_row = (
            await session.execute(
                select(IntradayRecoveryRun)
                .order_by(desc(IntradayRecoveryRun.created_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        if rec_row is not None:
            payload = rec_row.payload if isinstance(rec_row.payload, dict) else {}
            recon = payload.get("recon") if isinstance(payload.get("recon"), dict) else {}
            lifecycle_sync = (
                payload.get("lifecycle_sync")
                if isinstance(payload.get("lifecycle_sync"), dict)
                else {}
            )
            latest_recovery = {
                "id": str(rec_row.id),
                "emergency_stop": bool(rec_row.emergency_stop),
                "new_orders_allowed": bool(rec_row.new_orders_allowed),
                "actions": rec_row.actions or [],
                "reconciliation_result": recon.get("result"),
                "lifecycle_sync": lifecycle_sync,
                "created_at": rec_row.created_at.isoformat() if rec_row.created_at else None,
            }
    except Exception:  # noqa: BLE001
        latest_recovery = None

    active_alerts: list[dict[str, Any]] = []
    try:
        arows = list(
            (
                await session.execute(
                    select(AlertRecordModel)
                    .where(AlertRecordModel.status == "active")
                    .order_by(desc(AlertRecordModel.detected_at))
                    .limit(8)
                )
            )
            .scalars()
            .all()
        )
        for a in arows:
            active_alerts.append(
                {
                    "id": str(a.id),
                    "severity": a.severity,
                    "code": a.alert_type,
                    "message": a.message,
                    "detected_at": a.detected_at.isoformat() if a.detected_at else None,
                }
            )
    except Exception:  # noqa: BLE001
        active_alerts = []

    overnight_reviews: list[dict[str, Any]] = []
    try:
        orows = list(
            (
                await session.execute(
                    select(OvernightReview)
                    .order_by(desc(OvernightReview.created_at))
                    .limit(12)
                )
            )
            .scalars()
            .all()
        )
        for o in orows:
            overnight_reviews.append(
                {
                    "id": str(o.id),
                    "symbol": o.symbol,
                    "status": o.status,
                    "reasons": o.reasons or [],
                    "valid_for_session_date": o.valid_for_session_date,
                    "created_at": o.created_at.isoformat() if o.created_at else None,
                }
            )
    except Exception:  # noqa: BLE001
        overnight_reviews = []

    closing_intents: list[dict[str, Any]] = []
    try:
        irows = list(
            (
                await session.execute(
                    select(OrderIntent).order_by(desc(OrderIntent.created_at)).limit(30)
                )
            )
            .scalars()
            .all()
        )
        for intent in irows:
            meta = intent.metadata_json or {}
            thesis = intent.thesis or ""
            if meta.get("source") == "closing_service" or thesis.startswith("closing:"):
                closing_intents.append(
                    {
                        "id": str(intent.id),
                        "symbol": intent.symbol,
                        "side": intent.side,
                        "quantity": intent.quantity,
                        "status": intent.status,
                        "thesis": thesis,
                        "created_at": intent.created_at.isoformat() if intent.created_at else None,
                    }
                )
        closing_intents = closing_intents[:8]
    except Exception:  # noqa: BLE001
        closing_intents = []

    hard_stop_intents: list[dict[str, Any]] = []
    try:
        irows = list(
            (
                await session.execute(
                    select(OrderIntent).order_by(desc(OrderIntent.created_at)).limit(30)
                )
            )
            .scalars()
            .all()
        )
        for intent in irows:
            meta = intent.metadata_json or {}
            thesis = (intent.thesis or "").lower()
            if meta.get("reason") == "hard_stop" or thesis == "hard_stop":
                hard_stop_intents.append(
                    {
                        "id": str(intent.id),
                        "symbol": intent.symbol,
                        "side": intent.side,
                        "quantity": intent.quantity,
                        "status": intent.status,
                        "thesis": intent.thesis,
                        "created_at": intent.created_at.isoformat() if intent.created_at else None,
                    }
                )
        hard_stop_intents = hard_stop_intents[:8]
    except Exception:  # noqa: BLE001
        hard_stop_intents = []

    return {
        "as_of": dual_timezone_labels(now),
        "market_status": {
            "trading_state": controls.state.value,
            "new_orders_allowed": trading_controls.is_new_order_allowed(),
            "reason": controls.reason,
            "us_session": us_session,
            "au_session": au_session,
            "venue_sessions": venue_sessions,
            "primary_venue": primary,
            "enabled_venues": [v.value for v in enabled_venues(settings)],
            "active_ops_venue": ops_target.get("active_ops_venue"),
            "venue_phases": ops_target.get("venue_phases") or {},
            "pause_and_emergency_global": True,
            "workflow": workflow_summary,
            "workflows_by_venue": workflows_by_venue,
        },
        "universe": universe_summary,
        "force_close": force_close_ops,
        "hard_stop": hard_stop_ops,
        "monitor_positions": monitor_positions,
        "pending_events": pending_events,
        "hard_stop_intents": hard_stop_intents,
        "latest_closing": latest_closing,
        "latest_settlement": latest_settlement,
        "latest_reconciliation": latest_reconciliation,
        "latest_recovery": latest_recovery,
        "active_alerts": active_alerts,
        "overnight_reviews": overnight_reviews,
        "closing_intents": closing_intents,
        "portfolio": None
        if snap is None
        else {
            "equity": snap.equity,
            "cash": snap.cash,
            "cash_pct": snap.cash_pct,
            "gross_exposure_pct": snap.gross_exposure_pct,
            "daily_pnl": snap.daily_pnl,
            "daily_pnl_pct": snap.daily_pnl_pct,
            "drawdown_pct": snap.drawdown_pct,
            "open_positions": snap.open_positions,
            "base_currency": (snap.payload or {}).get("base_currency"),
            "cash_by_currency": (snap.payload or {}).get("cash_by_currency") or {},
            "venue_books": (snap.payload or {}).get("venue_books") or {},
        },
        "positions": [
            {
                "symbol": p.symbol,
                "quantity": p.quantity,
                "market_value": p.market_value,
                "unrealized_pnl": p.unrealized_pnl,
                "sector": p.sector,
                "venue": getattr(p, "venue", None) or "US",
                "currency": getattr(p, "currency", None),
                "exchange": getattr(p, "exchange", None),
            }
            for p in positions
        ],
        "open_orders": [
            {
                "id": str(o.id),
                "symbol": o.symbol,
                "side": o.side,
                "qty": o.qty,
                "status": o.status,
            }
            for o in open_orders
        ],
        "cio": None
        if latest_decision is None
        else {
            "decision_id": str(latest_decision.decision_id),
            "timestamp": latest_decision.decision_timestamp.isoformat(),
            "market_regime": latest_decision.market_regime,
            "portfolio_action": latest_decision.portfolio_action,
            "risk_approval": latest_decision.risk_approval,
            "payload": latest_decision.payload,
        },
        "agents": agent_latest,
        "agent_lamps": agent_lamps,
        "news": [
            {
                "headline": n.headline,
                "source": n.source,
                "published_at": n.published_at.isoformat(),
                "symbols": n.symbols,
            }
            for n in news
        ],
        "errors": [
            {
                "level": e.level,
                "event_type": e.event_type,
                "message": e.message,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in errors
        ],
        "next_jobs": _enriched_next_jobs(),
        "session_jobs": session_jobs,
        "committee_watch": committee_watch,
        "llm_budget": snapshot_llm_budget().to_dict(),
    }
