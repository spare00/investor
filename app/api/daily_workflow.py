"""Daily workflow + operations APIs (Phase 3, no broker orders)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db_session
from app.core.scheduler import upcoming_jobs
from app.execution.ops_persistence import persist_trading_controls
from app.execution.safety_controls import trading_controls
from app.models import DailyWorkflowRun
from app.workflow.daily import DailyWorkflowError, DailyWorkflowService
from app.workflow.recovery import RecoveryService
from app.workflow.states import ClosingPolicy
from sqlalchemy import select

router = APIRouter(tags=["daily-workflow"])


def _svc(session: AsyncSession) -> DailyWorkflowService:
    return DailyWorkflowService(session, settings=get_settings())


@router.get("/workflow/daily/current")
async def daily_current(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    run = await _svc(session).get_current()
    if run is None:
        return {"run": None}
    return {"run": _svc(session)._run_dict(run)}


@router.get("/workflow/daily/{workflow_run_id}")
async def daily_get(
    workflow_run_id: UUID, session: AsyncSession = Depends(get_db_session)
) -> dict[str, Any]:
    run = (
        await session.execute(select(DailyWorkflowRun).where(DailyWorkflowRun.id == workflow_run_id))
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="not_found")
    return {"run": _svc(session)._run_dict(run)}


@router.get("/workflow/daily/{workflow_run_id}/transitions")
async def daily_transitions(
    workflow_run_id: UUID, session: AsyncSession = Depends(get_db_session)
) -> dict[str, Any]:
    return {"transitions": await _svc(session).list_transitions(workflow_run_id)}


@router.get("/scheduler/jobs")
async def scheduler_jobs(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    planned = await _svc(session).planned_jobs()
    return {
        "enable_scheduler": get_settings().enable_scheduler,
        "runtime_jobs": upcoming_jobs(),
        "planned_jobs": planned,
    }


@router.post("/workflow/daily/prepare")
async def daily_prepare(
    session: AsyncSession = Depends(get_db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    try:
        result = await _svc(session).prepare()
        await session.commit()
        return {**result, "idempotency_key": idempotency_key, "broker_orders": False}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/workflow/daily/run-analysis")
async def daily_run_analysis(
    fake_llm: bool = False,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    try:
        result = await _svc(session).run_analysis(fake_llm=fake_llm)
        await session.commit()
        return result
    except DailyWorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/workflow/daily/revalidate")
async def daily_revalidate(
    fake_llm: bool = False,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    try:
        result = await _svc(session).revalidate(fake_llm=fake_llm)
        await session.commit()
        return result
    except DailyWorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/workflow/daily/evaluate-intraday")
async def daily_intraday(
    trigger: str = "interval",
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    try:
        result = await _svc(session).evaluate_intraday(trigger=trigger)
        await session.commit()
        return result
    except DailyWorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/workflow/daily/start-closing")
async def daily_closing(
    policy: str = ClosingPolicy.CLOSE_INTRADAY_ONLY.value,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    try:
        result = await _svc(session).start_closing(policy=ClosingPolicy(policy))
        await session.commit()
        return result
    except DailyWorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/workflow/daily/run-postmarket")
async def daily_postmarket(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    try:
        result = await _svc(session).run_postmarket()
        await session.commit()
        return result
    except DailyWorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/operations/pause")
async def operations_pause(
    reason: str = "operator", session: AsyncSession = Depends(get_db_session)
) -> dict[str, Any]:
    snap = trading_controls.pause(reason)
    await persist_trading_controls(session, trading_controls, changed_by="operations")
    await session.commit()
    return {"state": snap.state.value, "reason": snap.reason}


@router.post("/operations/resume")
async def operations_resume(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    snap = trading_controls.resume()
    await persist_trading_controls(session, trading_controls, changed_by="operations")
    await session.commit()
    return {"state": snap.state.value}


@router.post("/operations/emergency-stop")
async def operations_emergency_stop(
    reason: str = "operator", session: AsyncSession = Depends(get_db_session)
) -> dict[str, Any]:
    from app.brokers.factory import get_broker
    from app.core.config import get_settings

    settings = get_settings()
    snap = trading_controls.emergency_stop(reason)
    await persist_trading_controls(session, trading_controls, changed_by="operations")
    from app.alerts.ops import emit_emergency_stop_alert

    await emit_emergency_stop_alert(session, settings, reason=reason, source="operations_api")
    canceled = 0
    closed = 0
    error = None
    try:
        broker = get_broker(settings)
        if settings.emergency_stop_cancel_open_orders and hasattr(broker, "cancel_all_orders"):
            canceled = await broker.cancel_all_orders()
        if settings.emergency_stop_close_positions and hasattr(broker, "close_all_positions"):
            closed = await broker.close_all_positions()
    except Exception as exc:  # noqa: BLE001
        error = str(exc)[:300]
    await session.commit()
    return {
        "state": snap.state.value,
        "reason": snap.reason,
        "canceled_count": canceled,
        "closed_positions": closed,
        "close_positions_enabled": settings.emergency_stop_close_positions,
        "error": error,
        "broker_orders": False,
    }


@router.post("/operations/emergency-stop/clear")
async def operations_emergency_clear(
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    snap = trading_controls.clear_emergency()
    await persist_trading_controls(session, trading_controls, changed_by="operations")
    await session.commit()
    return {"state": snap.state.value}


@router.post("/operations/recovery")
async def operations_recovery(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    result = await RecoveryService(session).run()
    await session.commit()
    return result
