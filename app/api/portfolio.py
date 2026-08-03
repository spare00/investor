"""Portfolio / positions / orders API."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.execution.order_manager import OrderManager
from app.execution.position_manager import PositionManager

router = APIRouter(tags=["portfolio"])


@router.get("/portfolio")
async def get_portfolio(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    pm = PositionManager(session)
    try:
        synced = await pm.sync_from_broker()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"broker_sync_failed: {exc}") from exc
    state = await pm.portfolio_state_input()
    return {
        "synced": synced,
        "equity": state.equity,
        "cash": state.cash,
        "cash_pct": state.cash_pct,
        "gross_exposure_pct": state.gross_exposure_pct,
        "daily_pnl_pct": state.daily_pnl_pct,
        "positions": [p.model_dump() for p in state.positions],
    }


@router.get("/positions")
async def get_positions(session: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    pm = PositionManager(session)
    try:
        await pm.sync_from_broker()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    state = await pm.portfolio_state_input()
    return {"positions": [p.model_dump() for p in state.positions]}


@router.get("/orders")
async def get_orders(
    limit: int = 50,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    om = OrderManager(session)
    try:
        sync = await om.sync_statuses_from_broker()
    except Exception as exc:  # noqa: BLE001
        sync = {"error": str(exc)}
    rows = await om.list_orders(limit=limit)
    return {
        "sync": sync,
        "orders": [
            {
                "id": str(o.id),
                "broker_order_id": o.broker_order_id,
                "symbol": o.symbol,
                "side": o.side,
                "qty": o.qty,
                "order_type": o.order_type,
                "limit_price": o.limit_price,
                "status": o.status,
                "idempotency_key": o.idempotency_key,
                "submitted_at": o.submitted_at.isoformat() if o.submitted_at else None,
            }
            for o in rows
        ]
    }


@router.post("/orders/{order_id}/cancel")
async def cancel_order(
    order_id: UUID,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    try:
        row = await OrderManager(session).cancel_order(order_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "id": str(row.id),
        "status": row.status,
        "broker_order_id": row.broker_order_id,
    }
