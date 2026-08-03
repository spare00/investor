"""Execution intents, approvals, risk checks, reconciliation API."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.brokers.errors import BrokerError
from app.core.config import get_settings
from app.core.database import get_db_session
from app.execution.reconciliation import ReconciliationService
from app.execution.safety_controls import trading_controls
from app.execution.service import ExecutionService
from app.models import Order, OrderApproval, OrderIntent, PretradeRiskCheck
from app.risk import PortfolioRiskView
from app.schemas.cio import CIODecision

router = APIRouter(prefix="/execution", tags=["execution"])


class BuildIntentsBody(BaseModel):
    decision: dict[str, Any]
    equity: float = 25_000.0
    cash: float = 25_000.0
    gross_exposure: float = 0.0
    latest_prices: dict[str, float] = Field(default_factory=dict)
    data_quality_score: float = 1.0


class ValidateBody(BaseModel):
    equity: float = 25_000.0
    cash: float = 25_000.0
    buying_power: float = 25_000.0
    gross_exposure: float = 0.0
    position_qty: float = 0.0
    data_quality_score: float = 1.0
    quote_age_seconds: float = 0.0
    spread_bps: float = 10.0
    hard_vetoes: list[str] = Field(default_factory=list)
    market_open: bool = True
    asset_tradable: bool = True


class ActorBody(BaseModel):
    actor: str = "operator"
    reason: str = ""


def _intent_dict(i: OrderIntent) -> dict[str, Any]:
    return {
        "intent_id": str(i.id),
        "decision_id": str(i.decision_id) if i.decision_id else None,
        "symbol": i.symbol,
        "intent_type": i.intent_type,
        "side": i.side,
        "quantity": i.quantity,
        "approved_quantity": i.approved_quantity,
        "entry_price": i.entry_price,
        "stop_price": i.stop_price,
        "status": i.status,
        "client_order_id": i.client_order_id,
        "thesis": i.thesis,
        "expires_at": i.expires_at.isoformat() if i.expires_at else None,
        "exit_policy": i.exit_policy,
        "risk_check_id": str(i.risk_check_id) if i.risk_check_id else None,
        "approval_id": str(i.approval_id) if i.approval_id else None,
    }


@router.get("/intents")
async def list_intents(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    rows = await ExecutionService(session).list_intents()
    return {"intents": [_intent_dict(i) for i in rows]}


@router.get("/intents/{intent_id}")
async def get_intent(intent_id: UUID, session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    row = await session.get(OrderIntent, intent_id)
    if row is None:
        raise HTTPException(status_code=404, detail="intent_not_found")
    return _intent_dict(row)


@router.get("/approvals")
async def list_approvals(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    rows = list((await session.execute(select(OrderApproval).order_by(OrderApproval.created_at.desc()).limit(100))).scalars())
    return {
        "approvals": [
            {
                "approval_id": str(a.id),
                "intent_id": str(a.intent_id),
                "status": a.status,
                "acted_by": a.acted_by,
                "expires_at": a.expires_at.isoformat() if a.expires_at else None,
                "reason": a.reason,
            }
            for a in rows
        ]
    }


@router.get("/risk-checks")
async def list_risk_checks(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    rows = list(
        (await session.execute(select(PretradeRiskCheck).order_by(PretradeRiskCheck.created_at.desc()).limit(100))).scalars()
    )
    return {
        "risk_checks": [
            {
                "risk_check_id": str(r.id),
                "intent_id": str(r.intent_id),
                "status": r.status,
                "payload": r.payload,
            }
            for r in rows
        ]
    }


@router.get("/reconciliation")
async def list_reconciliation(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    from app.models import BrokerReconciliationRun

    rows = list(
        (
            await session.execute(
                select(BrokerReconciliationRun).order_by(BrokerReconciliationRun.created_at.desc()).limit(20)
            )
        )
        .scalars()
        .all()
    )
    return {
        "runs": [
            {
                "id": str(r.id),
                "sync_type": r.sync_type,
                "result": r.result,
                "issues": r.issues,
            }
            for r in rows
        ]
    }


@router.post("/intents/build")
async def build_intents(
    body: BuildIntentsBody, session: AsyncSession = Depends(get_db_session)
) -> dict[str, Any]:
    if trading_controls.snapshot().state.value == "emergency_stop":
        raise HTTPException(status_code=423, detail="emergency_stop_active")
    try:
        decision = CIODecision.model_validate(body.decision)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"invalid_decision:{exc}") from exc
    portfolio = PortfolioRiskView(
        equity=body.equity,
        cash=body.cash,
        cash_pct=(body.cash / body.equity * 100.0) if body.equity else 100.0,
        gross_exposure_pct=(body.gross_exposure / body.equity * 100.0) if body.equity else 0.0,
    )
    svc = ExecutionService(session)
    intents = await svc.build_intents_from_decision(
        decision,
        portfolio=portfolio,
        latest_prices=body.latest_prices,
        data_quality_score=body.data_quality_score,
    )
    await session.commit()
    return {"intents": [_intent_dict(i) for i in intents]}


@router.post("/intents/{intent_id}/validate")
async def validate_intent(
    intent_id: UUID, body: ValidateBody, session: AsyncSession = Depends(get_db_session)
) -> dict[str, Any]:
    svc = ExecutionService(session)
    try:
        result = await svc.validate_intent(
            intent_id,
            equity=body.equity,
            cash=body.cash,
            buying_power=body.buying_power,
            gross_exposure=body.gross_exposure,
            position_qty=body.position_qty,
            data_quality_score=body.data_quality_score,
            quote_age_seconds=body.quote_age_seconds,
            spread_bps=body.spread_bps,
            hard_vetoes=body.hard_vetoes,
            market_open=body.market_open,
            asset_tradable=body.asset_tradable,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return result.to_dict()


@router.post("/intents/{intent_id}/approve")
async def approve_intent(
    intent_id: UUID, body: ActorBody, session: AsyncSession = Depends(get_db_session)
) -> dict[str, Any]:
    if trading_controls.snapshot().state.value == "emergency_stop":
        raise HTTPException(status_code=423, detail="emergency_stop_active")
    try:
        intent = await ExecutionService(session).approve_intent(intent_id, actor=body.actor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return _intent_dict(intent)


@router.post("/intents/{intent_id}/reject")
async def reject_intent(
    intent_id: UUID, body: ActorBody, session: AsyncSession = Depends(get_db_session)
) -> dict[str, Any]:
    intent = await ExecutionService(session).reject_intent(
        intent_id, actor=body.actor, reason=body.reason
    )
    await session.commit()
    return _intent_dict(intent)


@router.post("/intents/{intent_id}/submit")
async def submit_intent(
    intent_id: UUID, session: AsyncSession = Depends(get_db_session)
) -> dict[str, Any]:
    settings = get_settings()
    if not settings.enable_broker_orders:
        raise HTTPException(status_code=403, detail="enable_broker_orders_false")
    try:
        order = await ExecutionService(session).submit_intent(intent_id)
    except (BrokerError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    if order is None:
        return {"submitted": False}
    return {
        "submitted": True,
        "order_id": str(order.id),
        "broker_order_id": order.broker_order_id,
        "status": order.status,
        "client_order_id": order.idempotency_key,
    }


@router.post("/orders/{order_id}/cancel")
async def cancel_order(order_id: str, session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    from app.brokers.factory import get_broker

    settings = get_settings()
    if not settings.enable_broker_orders:
        raise HTTPException(status_code=403, detail="enable_broker_orders_false")
    broker = get_broker(settings)
    try:
        result = await broker.cancel_order(order_id)
    except BrokerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row = (
        await session.execute(select(Order).where(Order.broker_order_id == order_id))
    ).scalar_one_or_none()
    if row:
        row.status = result.status.value
    await session.commit()
    return {"broker_order_id": result.broker_order_id, "status": result.status.value}


@router.post("/orders/{order_id}/replace")
async def replace_order(
    order_id: str,
    qty: float,
    limit_price: float | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    from app.brokers.base import OrderRequest, OrderSide
    from app.brokers.factory import get_broker

    settings = get_settings()
    if not settings.enable_broker_orders:
        raise HTTPException(status_code=403, detail="enable_broker_orders_false")
    row = (
        await session.execute(select(Order).where(Order.broker_order_id == order_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="order_not_found")
    broker = get_broker(settings)
    try:
        result = await broker.replace_order(  # type: ignore[attr-defined]
            order_id,
            OrderRequest(
                symbol=row.symbol,
                side=OrderSide(row.side),
                qty=qty,
                order_type=row.order_type,
                limit_price=limit_price if limit_price is not None else row.limit_price,
                stop_price=row.stop_price,
                idempotency_key=f"{row.idempotency_key}-r",
            ),
        )
    except BrokerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row.status = "replaced"
    row.raw_payload = {**(row.raw_payload or {}), "replaced_by": result.broker_order_id}
    await session.commit()
    return {
        "original_order_id": order_id,
        "replacement_order_id": result.broker_order_id,
        "status": result.status.value,
    }


@router.post("/reconcile")
async def reconcile(
    sync_type: str = "ON_DEMAND", session: AsyncSession = Depends(get_db_session)
) -> dict[str, Any]:
    result = await ReconciliationService(session).run(sync_type=sync_type)
    await session.commit()
    return result


@router.post("/cancel-all")
async def cancel_all(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    from app.brokers.factory import get_broker

    settings = get_settings()
    if not settings.enable_broker_orders and settings.broker_provider != "mock":
        raise HTTPException(status_code=403, detail="enable_broker_orders_false")
    broker = get_broker(settings)
    n = await broker.cancel_all_orders()  # type: ignore[attr-defined]
    await session.commit()
    return {"canceled": n}
