"""Dashboard and audit read APIs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db_session
from app.core.metrics import metrics_payload
from app.core.scheduler import upcoming_jobs
from app.core.timeutils import dual_timezone_labels, utc_now
from app.execution.order_manager import OrderManager
from app.execution.safety_controls import trading_controls
from app.agents.activity import (
    AGENT_ORDER,
    AGENT_SHORT,
    classify_agent_lamp,
    snapshot_agent_activity,
)
from app.market.calendar import MarketCalendarService
from app.services.llm_budget import snapshot_llm_budget
from app.models import (
    AgentReport,
    AgentRun,
    CIODecisionRecord,
    NewsItem,
    Order,
    PortfolioSnapshot,
    Position,
    SystemEvent,
)
from app.workflow.daily import DailyWorkflowService
from fastapi.responses import Response

router = APIRouter(tags=["dashboard"])


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


@router.get("/dashboard/summary")
async def dashboard_summary(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    """Single payload for the ops dashboard."""
    now = utc_now()
    controls = trading_controls.snapshot()

    # Keep local order statuses aligned with Alpaca before rendering open orders.
    try:
        await OrderManager(session).sync_statuses_from_broker()
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
    us_session = MarketCalendarService(settings).get_market_status(now).to_dict()
    workflow_summary: dict[str, Any] | None = None
    try:
        run = await DailyWorkflowService(session, settings=settings).get_current()
        if run is not None:
            workflow_summary = {
                "session_date": run.session_date,
                "state": run.current_state,
                "status": run.status,
            }
    except Exception:  # noqa: BLE001 — dashboard should still render
        workflow_summary = None

    universe_summary: dict[str, Any] | None = None
    try:
        from app.universe.service import UniverseService

        universe_summary = await UniverseService(session, settings=settings).snapshot()
    except Exception:  # noqa: BLE001
        universe_summary = None

    return {
        "as_of": dual_timezone_labels(now),
        "market_status": {
            "trading_state": controls.state.value,
            "new_orders_allowed": trading_controls.is_new_order_allowed(),
            "reason": controls.reason,
            "us_session": us_session,
            "workflow": workflow_summary,
        },
        "universe": universe_summary,
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
        },
        "positions": [
            {
                "symbol": p.symbol,
                "quantity": p.quantity,
                "market_value": p.market_value,
                "unrealized_pnl": p.unrealized_pnl,
                "sector": p.sector,
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
        "next_jobs": upcoming_jobs(),
        "llm_budget": snapshot_llm_budget().to_dict(),
    }
