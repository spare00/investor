"""Trading control API — pause / resume / emergency stop."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.execution.safety_controls import trading_controls

router = APIRouter(prefix="/trading", tags=["trading"])


@router.get("/state")
async def trading_state() -> dict[str, Any]:
    snap = trading_controls.snapshot()
    return {
        "state": snap.state.value,
        "reason": snap.reason,
        "changed_at": snap.changed_at.isoformat(),
        "new_orders_allowed": trading_controls.is_new_order_allowed(),
        "canceled_open_orders": snap.canceled_open_orders,
    }


@router.post("/pause")
async def pause_trading(reason: str = "manual_pause") -> dict[str, Any]:
    snap = trading_controls.pause(reason=reason)
    return {"state": snap.state.value, "reason": snap.reason, "changed_at": snap.changed_at.isoformat()}


@router.post("/resume")
async def resume_trading(reason: str = "manual_resume") -> dict[str, Any]:
    snap = trading_controls.resume(reason=reason)
    return {
        "state": snap.state.value,
        "reason": snap.reason,
        "changed_at": snap.changed_at.isoformat(),
        "note": (
            "Emergency stop cannot be cleared via resume; use /trading/clear-emergency first"
            if snap.state.value == "emergency_stop"
            else None
        ),
    }


@router.post("/emergency-stop")
async def emergency_stop(reason: str = "emergency_stop") -> dict[str, Any]:
    """Block all new orders and cancel open broker orders when possible."""
    from app.core.database import get_session_factory
    from app.execution.order_manager import OrderManager

    snap = trading_controls.emergency_stop(reason=reason)
    canceled = 0
    error = None
    try:
        factory = get_session_factory()
        async with factory() as session:
            om = OrderManager(session)
            # controls already emergency-stopped; cancel via broker directly
            canceled = await om.broker.cancel_all_orders()  # type: ignore[attr-defined]
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
    return {
        "state": snap.state.value,
        "reason": snap.reason,
        "changed_at": snap.changed_at.isoformat(),
        "canceled_open_orders": True,
        "canceled_count": canceled,
        "error": error,
        "action": "cancel_open_orders_and_block_new",
    }


@router.post("/clear-emergency")
async def clear_emergency(reason: str = "emergency_cleared") -> dict[str, Any]:
    """Move emergency → paused. Requires a subsequent /resume to trade again."""
    snap = trading_controls.clear_emergency(reason=reason)
    return {
        "state": snap.state.value,
        "reason": snap.reason,
        "changed_at": snap.changed_at.isoformat(),
        "next": "POST /trading/resume to re-enable new orders",
    }
