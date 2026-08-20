"""Phase 7 performance metrics API — read-only portfolio analytics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db_session
from app.models import MetricCalculationRun, Order
from app.performance.service import PerformanceService
from app.providers.registry import list_providers

router = APIRouter(tags=["performance"])


class RecalculateBody(BaseModel):
    period_start: datetime | None = None
    period_end: datetime | None = None
    benchmark: str | None = None
    risk_free_rate: float | None = None


class EvaluateDecisionsBody(BaseModel):
    period_start: datetime | None = None
    period_end: datetime | None = None
    limit: int = Field(default=50, ge=1, le=500)


class EvaluateAgentsBody(BaseModel):
    period_start: datetime | None = None
    period_end: datetime | None = None


def _period(
    period_start: datetime | None,
    period_end: datetime | None,
    *,
    days: int = 90,
) -> tuple[datetime, datetime]:
    end = period_end or datetime.now(UTC)
    start = period_start or (end - timedelta(days=days))
    return start, end


def _svc(session: AsyncSession) -> PerformanceService:
    return PerformanceService(session, settings=get_settings())


@router.get("/performance/portfolio")
async def performance_portfolio(
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    benchmark: str | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    start, end = _period(period_start, period_end)
    settings = get_settings()
    return await _svc(session).portfolio_summary(
        start, end, benchmark_name=benchmark or settings.primary_benchmark
    )


@router.get("/performance/returns")
async def performance_returns(
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    benchmark: str | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    start, end = _period(period_start, period_end)
    settings = get_settings()
    return await _svc(session).returns_summary(
        start, end, benchmark_name=benchmark or settings.primary_benchmark
    )


@router.get("/performance/risk")
async def performance_risk(
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    benchmark: str | None = None,
    risk_free_rate: float | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    start, end = _period(period_start, period_end)
    settings = get_settings()
    return await _svc(session).risk_summary(
        start,
        end,
        benchmark_name=benchmark or settings.primary_benchmark,
        risk_free_rate=risk_free_rate if risk_free_rate is not None else settings.risk_free_rate_annual,
    )


@router.get("/performance/drawdowns")
async def performance_drawdowns(
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    start, end = _period(period_start, period_end)
    return await _svc(session).drawdowns(start, end)


@router.get("/performance/trades")
async def performance_trades(
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    start, end = _period(period_start, period_end)
    return await _svc(session).trade_metrics(start, end)


@router.get("/performance/execution")
async def performance_execution(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    rows = list((await session.execute(select(Order))).scalars().all())

    def _st(order: Order) -> str:
        return str(order.status or "").strip().lower()

    filled = [o for o in rows if _st(o) in {"filled", "partially_filled"}]
    partial = [o for o in rows if _st(o) == "partially_filled"]
    cancelled = [o for o in rows if _st(o) in {"canceled", "cancelled"}]
    rejected = [o for o in rows if _st(o) == "rejected"]
    stats = {
        "total_orders": len(rows),
        "filled": len(filled),
        "partial": len(partial),
        "cancelled": len(cancelled),
        "rejected": len(rejected),
    }
    return _svc(session).execution(stats)


@router.get("/performance/decisions")
async def performance_decisions(
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    start, end = _period(period_start, period_end)
    return await _svc(session).evaluate_decisions_batch(start, end, limit=limit)


@router.get("/performance/agents")
async def performance_agents(
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    start, end = _period(period_start, period_end)
    return await _svc(session).agents(start, end)


@router.get("/performance/calibration")
async def performance_calibration(
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    svc = _svc(session)
    grouped = await svc.calibration_samples_by_horizon()
    samples = list(grouped.pop("_all", []))
    return svc.calibration(
        samples,
        min_sample_size=get_settings().min_calibration_sample_size,
        by_horizon=grouped,
    )


@router.get("/performance/providers")
async def performance_providers(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    settings = get_settings()
    stats = {
        "providers": list_providers(settings),
        "note": "Reliability counters from Prometheus are not scraped in-process; use fixture health.",
    }
    return _svc(session).providers(stats)


@router.post("/performance/recalculate")
async def performance_recalculate(
    body: RecalculateBody | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    body = body or RecalculateBody()
    start, end = _period(body.period_start, body.period_end)
    settings = get_settings()
    svc = _svc(session)
    existing = svc.last_run_for_period(start, end)
    if existing is not None:
        return {"run_id": existing["run_id"], "status": "reused", "result": existing}

    result = await svc.recalculate(
        start,
        end,
        benchmark_name=body.benchmark or settings.primary_benchmark,
        risk_free_rate=body.risk_free_rate if body.risk_free_rate is not None else settings.risk_free_rate_annual,
    )
    run_row = MetricCalculationRun(
        id=UUID(result["run_id"]),
        metric_scope="portfolio",
        period_start=start,
        period_end=end,
        started_at=datetime.fromisoformat(result["started_at"]),
        completed_at=datetime.fromisoformat(result["completed_at"]),
        status=result["status"],
        calculation_version=result["calculation_version"],
        records_processed=result.get("records_processed", 0),
        payload={"sections": list(k for k in result if k not in {"run_id", "status"})},
    )
    session.add(run_row)
    await session.commit()
    return {"run_id": result["run_id"], "status": "completed", "result": result}


@router.post("/performance/evaluate-decisions")
async def performance_evaluate_decisions(
    body: EvaluateDecisionsBody | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    body = body or EvaluateDecisionsBody()
    start, end = _period(body.period_start, body.period_end)
    return await _svc(session).evaluate_decisions_batch(start, end, limit=body.limit, persist=True)


@router.post("/performance/evaluate-agents")
async def performance_evaluate_agents(
    body: EvaluateAgentsBody | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    body = body or EvaluateAgentsBody()
    start, end = _period(body.period_start, body.period_end)
    return await _svc(session).evaluate_agents_batch(start, end, persist=True)
