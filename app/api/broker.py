"""Broker status / account / positions / orders API (read-focused)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.brokers.errors import BrokerError
from app.brokers.factory import get_broker
from app.core.config import get_settings
from app.core.database import get_db_session
from app.models import Order
from sqlalchemy import select

router = APIRouter(prefix="/broker", tags=["broker"])


def _safe_account(raw: dict[str, Any]) -> dict[str, Any]:
    from app.brokers.models import redact_account_id

    out = dict(raw)
    if "id" in out:
        out["account_id_reference"] = redact_account_id(str(out.pop("id")))
    for key in ("account_number", "account_id"):
        if key in out:
            out[key] = redact_account_id(str(out[key]))
    return out


@router.get("/status")
async def broker_status() -> dict[str, Any]:
    settings = get_settings()
    try:
        broker = get_broker(settings)
        health = await broker.health_check() if hasattr(broker, "health_check") else None
        caps = broker.capabilities() if hasattr(broker, "capabilities") else None
        return {
            "provider": settings.broker_provider,
            "environment": settings.broker_environment,
            "enable_broker_connection": settings.enable_broker_connection,
            "enable_broker_orders": settings.enable_broker_orders,
            "enable_live_trading": settings.enable_live_trading,
            "require_manual_order_approval": settings.require_manual_order_approval,
            "health": None if health is None else health.model_dump(mode="json"),
            "capabilities": None if caps is None else caps.model_dump(),
        }
    except BrokerError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/account")
async def broker_account() -> dict[str, Any]:
    settings = get_settings()
    try:
        broker = get_broker(settings)
        if hasattr(broker, "get_account_canonical"):
            acct = await broker.get_account_canonical()
            return acct.model_dump(mode="json")
        raw = await broker.get_account()
        return _safe_account(dict(raw))
    except BrokerError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/positions")
async def broker_positions() -> dict[str, Any]:
    settings = get_settings()
    try:
        broker = get_broker(settings)
        if hasattr(broker, "get_positions_canonical"):
            rows = await broker.get_positions_canonical()
            return {"positions": [p.model_dump(mode="json") for p in rows]}
        return {"positions": await broker.get_positions()}
    except BrokerError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/orders")
async def broker_orders(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    settings = get_settings()
    local = list((await session.execute(select(Order).order_by(Order.created_at.desc()).limit(100))).scalars())
    remote: list[Any] = []
    try:
        broker = get_broker(settings)
        if hasattr(broker, "get_open_orders"):
            remote = await broker.get_open_orders()
    except BrokerError:
        remote = []
    return {
        "local_orders": [
            {
                "id": str(o.id),
                "broker_order_id": o.broker_order_id,
                "client_order_id": o.idempotency_key,
                "symbol": o.symbol,
                "side": o.side,
                "qty": o.qty,
                "status": o.status,
            }
            for o in local
        ],
        "broker_open_orders": [
            {
                "broker_order_id": getattr(r, "broker_order_id", None),
                "status": getattr(getattr(r, "status", None), "value", getattr(r, "status", None)),
                "filled_qty": getattr(r, "filled_qty", None),
            }
            for r in remote
        ],
    }


@router.get("/orders/{order_id}")
async def broker_order_detail(
    order_id: str, session: AsyncSession = Depends(get_db_session)
) -> dict[str, Any]:
    from uuid import UUID

    try:
        uid = UUID(order_id)
        row = await session.get(Order, uid)
        if row is not None:
            return {
                "id": str(row.id),
                "broker_order_id": row.broker_order_id,
                "client_order_id": row.idempotency_key,
                "symbol": row.symbol,
                "status": row.status,
                "qty": row.qty,
                "raw_payload": row.raw_payload,
            }
    except ValueError:
        pass
    settings = get_settings()
    try:
        broker = get_broker(settings)
        result = await broker.get_order(order_id)
        return {
            "broker_order_id": result.broker_order_id,
            "status": result.status.value,
            "filled_qty": result.filled_qty,
            "avg_fill_price": result.avg_fill_price,
        }
    except BrokerError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
