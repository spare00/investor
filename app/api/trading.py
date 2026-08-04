"""Trading control API — pause / resume / emergency stop."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.core.config import get_settings
from app.execution.ops_persistence import persist_trading_controls
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
    from app.core.database import get_session_factory

    snap = trading_controls.pause(reason=reason)
    try:
        factory = get_session_factory()
        async with factory() as session:
            await persist_trading_controls(session, trading_controls, changed_by="api")
            await session.commit()
    except Exception:  # noqa: BLE001
        pass
    return {"state": snap.state.value, "reason": snap.reason, "changed_at": snap.changed_at.isoformat()}


@router.post("/resume")
async def resume_trading(reason: str = "manual_resume") -> dict[str, Any]:
    from app.core.database import get_session_factory

    snap = trading_controls.resume(reason=reason)
    try:
        factory = get_session_factory()
        async with factory() as session:
            await persist_trading_controls(session, trading_controls, changed_by="api")
            await session.commit()
    except Exception:  # noqa: BLE001
        pass
    return {
        "state": snap.state.value,
        "reason": snap.reason,
        "changed_at": snap.changed_at.isoformat(),
        "note": (
            "Emergency stop cannot be cleared via resume; use /trading/clear-emergency or /trading/restart"
            if snap.state.value == "emergency_stop"
            else None
        ),
    }


@router.post("/emergency-stop")
async def emergency_stop(reason: str = "emergency_stop") -> dict[str, Any]:
    """Block new orders; cancel open orders by default; never auto-close positions unless configured."""
    from app.core.database import get_session_factory
    from app.brokers.factory import get_broker

    settings = get_settings()
    snap = trading_controls.emergency_stop(reason=reason)
    canceled = 0
    closed_positions = 0
    error = None
    try:
        factory = get_session_factory()
        async with factory() as session:
            await persist_trading_controls(session, trading_controls, changed_by="api")
            broker = get_broker(settings)
            if settings.emergency_stop_cancel_open_orders and hasattr(broker, "cancel_all_orders"):
                canceled = await broker.cancel_all_orders()
            if settings.emergency_stop_close_positions and hasattr(broker, "close_all_positions"):
                closed_positions = await broker.close_all_positions()
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        error = str(exc)[:300]
    return {
        "state": snap.state.value,
        "reason": snap.reason,
        "changed_at": snap.changed_at.isoformat(),
        "canceled_open_orders": settings.emergency_stop_cancel_open_orders,
        "canceled_count": canceled,
        "closed_positions": closed_positions,
        "close_positions_enabled": settings.emergency_stop_close_positions,
        "error": error,
        "action": "cancel_open_orders_and_block_new",
    }


@router.post("/clear-emergency")
async def clear_emergency(reason: str = "emergency_cleared") -> dict[str, Any]:
    """Move emergency → paused. Requires a subsequent /resume to trade again."""
    from app.core.database import get_session_factory

    snap = trading_controls.clear_emergency(reason=reason)
    try:
        factory = get_session_factory()
        async with factory() as session:
            await persist_trading_controls(session, trading_controls, changed_by="api")
            await session.commit()
    except Exception:  # noqa: BLE001
        pass
    return {
        "state": snap.state.value,
        "reason": snap.reason,
        "changed_at": snap.changed_at.isoformat(),
        "next": "POST /trading/resume to re-enable new orders",
    }


@router.post("/restart")
async def restart_trading(reason: str = "dashboard_restart") -> dict[str, Any]:
    """One-click ops recovery: clear emergency if needed, then resume to active."""
    from app.core.database import get_session_factory

    steps: list[str] = []
    before = trading_controls.snapshot().state.value
    if before == "emergency_stop":
        trading_controls.clear_emergency(reason=f"{reason}:clear")
        steps.append("cleared_emergency")
    snap = trading_controls.resume(reason=reason)
    steps.append("resumed")
    try:
        factory = get_session_factory()
        async with factory() as session:
            await persist_trading_controls(session, trading_controls, changed_by="api")
            await session.commit()
    except Exception:  # noqa: BLE001
        pass
    return {
        "state": snap.state.value,
        "reason": snap.reason,
        "changed_at": snap.changed_at.isoformat(),
        "before": before,
        "steps": steps,
        "new_orders_allowed": trading_controls.is_new_order_allowed(),
    }
