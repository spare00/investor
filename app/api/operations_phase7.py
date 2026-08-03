"""Phase 7 operations API — metrics, alerts, readiness, simulations, backup."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.alerts.base import AlertStatus
from app.alerts.service import AlertService
from app.core.config import get_settings
from app.core.database import get_db_session
from app.models import ReadinessEvaluationRecord, SimulationRunRecord
from app.ops.backup import BackupService
from app.ops.readiness import GateEvaluator, ReadinessGate
from app.performance.service import PerformanceService
from app.simulation.runner import MultiDaySimulationRunner, SimulationScenario

router = APIRouter(tags=["operations-phase7"])

_active_simulations: dict[str, str] = {}


class ReadinessBody(BaseModel):
    gate: str | None = None
    operator_note: str | None = None


class SimulationRunBody(BaseModel):
    scenario: str = Field(default=SimulationScenario.BULL_MARKET.value)
    days: int = Field(default=5, ge=1, le=90)
    seed: int | None = None


class AlertAckBody(BaseModel):
    by: str = "operator"


@router.get("/operations/metrics")
async def operations_metrics(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    counters = {
        "jobs_total": 0,
        "jobs_success": 0,
        "jobs_failed": 0,
        "workflows_total": 0,
        "workflows_completed": 0,
        "alerts_fired": 0,
        "manual_interventions": 0,
        "uptime_seconds": 0,
        "window_seconds": 86400,
        "note": "Counters are placeholders until Prometheus scrape bridge ships",
    }
    kpis = PerformanceService(session).operational(counters)
    return {
        "counters": counters,
        "kpis": {
            k: (v.__dict__ if hasattr(v, "__dict__") else v)
            for k, v in kpis.items()
        },
    }


@router.get("/operations/alerts")
async def operations_alerts(
    status: str | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    svc = AlertService(session, settings=get_settings())
    st = AlertStatus(status) if status else None
    alerts = await svc.list_alerts(status=st)
    return {
        "alerts": [
            {
                "id": str(a.id),
                "code": a.code,
                "message": a.message,
                "severity": a.severity.value,
                "status": a.status.value,
                "created_at": a.created_at.isoformat(),
            }
            for a in alerts
        ]
    }


@router.get("/operations/alerts/{alert_id}")
async def operations_alert_detail(
    alert_id: UUID,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    svc = AlertService(session, settings=get_settings())
    for alert in await svc.list_alerts():
        if alert.id == alert_id:
            return {
                "id": str(alert.id),
                "code": alert.code,
                "message": alert.message,
                "severity": alert.severity.value,
                "status": alert.status.value,
                "context": alert.context,
                "created_at": alert.created_at.isoformat(),
                "acknowledged_at": None if alert.acknowledged_at is None else alert.acknowledged_at.isoformat(),
                "resolved_at": None if alert.resolved_at is None else alert.resolved_at.isoformat(),
            }
    raise HTTPException(404, "alert_not_found")


@router.post("/operations/alerts/{alert_id}/acknowledge")
async def operations_alert_ack(
    alert_id: UUID,
    body: AlertAckBody | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    body = body or AlertAckBody()
    svc = AlertService(session, settings=get_settings())
    result = await svc.acknowledge(alert_id, by=body.by)
    if not result.emitted:
        raise HTTPException(404 if result.reason == "not_found" else 409, result.reason)
    await session.commit()
    return {"alert_id": str(alert_id), "status": "acknowledged"}


@router.post("/operations/alerts/{alert_id}/resolve")
async def operations_alert_resolve(
    alert_id: UUID,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    svc = AlertService(session, settings=get_settings())
    result = await svc.resolve(alert_id)
    if not result.emitted:
        raise HTTPException(404 if result.reason == "not_found" else 409, result.reason)
    await session.commit()
    return {"alert_id": str(alert_id), "status": "resolved"}


@router.get("/operations/readiness")
async def operations_readiness(
    gate: str | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    evaluator = GateEvaluator(get_settings())
    g = ReadinessGate(gate) if gate else evaluator.default_gate()
    return evaluator.evaluate(g)


@router.post("/readiness/evaluate")
async def readiness_evaluate(
    body: ReadinessBody | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    body = body or ReadinessBody()
    evaluator = GateEvaluator(get_settings())
    gate = ReadinessGate(body.gate) if body.gate else evaluator.default_gate()
    result = evaluator.evaluate(gate)
    row = ReadinessEvaluationRecord(
        gate=gate.value,
        result=result,
        evaluated_at=datetime.now(UTC),
        operator_note=body.operator_note,
    )
    session.add(row)
    await session.commit()
    return {"evaluation_id": str(row.id), **result}


@router.get("/simulations")
async def list_simulations(
    limit: int = 20,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    rows = list(
        (
            await session.execute(
                select(SimulationRunRecord).order_by(desc(SimulationRunRecord.created_at)).limit(limit)
            )
        ).scalars().all()
    )
    return {
        "simulations": [
            {
                "id": str(r.id),
                "scenario": r.scenario,
                "trading_days": r.trading_days,
                "return_pct": r.return_pct,
                "max_drawdown": r.max_drawdown,
                "status": r.status,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
    }


@router.get("/simulations/{simulation_id}")
async def get_simulation(
    simulation_id: UUID,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    row = await session.get(SimulationRunRecord, simulation_id)
    if row is None:
        raise HTTPException(404, "simulation_not_found")
    return {
        "id": str(row.id),
        "scenario": row.scenario,
        "trading_days": row.trading_days,
        "return_pct": row.return_pct,
        "benchmark_return": row.benchmark_return,
        "max_drawdown": row.max_drawdown,
        "sharpe": row.sharpe,
        "sortino": row.sortino,
        "win_rate": row.win_rate,
        "trade_count": row.trade_count,
        "status": row.status,
        "code_version": row.code_version,
        "payload": row.payload,
    }


@router.post("/simulations/run")
async def run_simulation(
    body: SimulationRunBody,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    runner = MultiDaySimulationRunner(session, settings=get_settings(), seed=body.seed)
    summary = await runner.run(body.scenario, days=body.days)
    if hasattr(summary, "to_dict"):
        payload = summary.to_dict()
    else:
        payload = summary
    sim_id = payload.get("simulation_id")
    if sim_id:
        _active_simulations[str(sim_id)] = "COMPLETED"
    await session.commit()
    return {"simulation_id": sim_id, "status": "COMPLETED", "summary": payload}


@router.post("/simulations/{simulation_id}/cancel")
async def cancel_simulation(
    simulation_id: UUID,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    row = await session.get(SimulationRunRecord, simulation_id)
    if row is None:
        raise HTTPException(404, "simulation_not_found")
    if row.status in {"CANCELLED", "COMPLETED"}:
        return {"simulation_id": str(simulation_id), "status": row.status, "note": "already_terminal"}
    row.status = "CANCELLED"
    _active_simulations[str(simulation_id)] = "CANCELLED"
    await session.commit()
    return {"simulation_id": str(simulation_id), "status": "CANCELLED"}


@router.post("/backup/create")
async def backup_create(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    result = await BackupService(session=session).create(as_zip=True)
    return {
        "backup_id": result.backup_id,
        "path": result.path,
        "file_count": result.file_count,
        "created_at": result.created_at,
    }


@router.post("/backup/verify")
async def backup_verify(path: str) -> dict[str, Any]:
    verified = BackupService().verify(path)
    return {
        "valid": verified.valid,
        "backup_id": verified.backup_id,
        "errors": verified.errors,
    }
