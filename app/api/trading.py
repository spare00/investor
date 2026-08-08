"""Trading control API — pause / resume / emergency stop."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.core.config import get_settings
from app.core.logging import get_logger
from app.execution.ops_persistence import persist_trading_controls
from app.execution.safety_controls import trading_controls

logger = get_logger(__name__)
router = APIRouter(prefix="/trading", tags=["trading"])


async def _persist_controls(*, changed_by: str = "api") -> tuple[bool, str | None]:
    """Persist in-memory trading controls. Returns (ok, error)."""
    from app.core.database import get_session_factory

    try:
        factory = get_session_factory()
        async with factory() as session:
            await persist_trading_controls(session, trading_controls, changed_by=changed_by)
            await session.commit()
        return True, None
    except Exception as exc:  # noqa: BLE001
        logger.exception("trading_controls_persist_failed", changed_by=changed_by)
        return False, str(exc)[:300]


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
    persisted, persist_error = await _persist_controls(changed_by="api:pause")
    return {
        "state": snap.state.value,
        "reason": snap.reason,
        "changed_at": snap.changed_at.isoformat(),
        "persisted": persisted,
        "persist_error": persist_error,
    }


@router.post("/resume")
async def resume_trading(reason: str = "manual_resume") -> dict[str, Any]:
    snap = trading_controls.resume(reason=reason)
    persisted, persist_error = await _persist_controls(changed_by="api:resume")
    return {
        "state": snap.state.value,
        "reason": snap.reason,
        "changed_at": snap.changed_at.isoformat(),
        "persisted": persisted,
        "persist_error": persist_error,
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
    persisted = False
    try:
        factory = get_session_factory()
        async with factory() as session:
            await persist_trading_controls(session, trading_controls, changed_by="api")
            from app.alerts.ops import emit_emergency_stop_alert

            await emit_emergency_stop_alert(
                session, settings, reason=reason, source="trading_api"
            )
            broker = get_broker(settings)
            if settings.emergency_stop_cancel_open_orders and hasattr(broker, "cancel_all_orders"):
                canceled = await broker.cancel_all_orders()
            if settings.emergency_stop_close_positions and hasattr(broker, "close_all_positions"):
                closed_positions = await broker.close_all_positions()
            await session.commit()
            persisted = True
    except Exception as exc:  # noqa: BLE001
        error = str(exc)[:300]
        logger.exception("emergency_stop_persist_or_broker_failed")
    return {
        "state": snap.state.value,
        "reason": snap.reason,
        "changed_at": snap.changed_at.isoformat(),
        "canceled_open_orders": settings.emergency_stop_cancel_open_orders,
        "canceled_count": canceled,
        "closed_positions": closed_positions,
        "close_positions_enabled": settings.emergency_stop_close_positions,
        "error": error,
        "persisted": persisted,
        "action": "cancel_open_orders_and_block_new",
    }


@router.post("/clear-emergency")
async def clear_emergency(reason: str = "emergency_cleared") -> dict[str, Any]:
    """Move emergency → paused. Requires a subsequent /resume to trade again."""
    from app.core.database import get_session_factory

    snap = trading_controls.clear_emergency(reason=reason)
    resolved = 0
    persisted = False
    persist_error = None
    try:
        factory = get_session_factory()
        async with factory() as session:
            await persist_trading_controls(session, trading_controls, changed_by="api")
            from app.alerts.ops import resolve_alerts_by_code

            resolved = await resolve_alerts_by_code(
                session, get_settings(), code="trading.emergency_stop"
            )
            await session.commit()
            persisted = True
    except Exception as exc:  # noqa: BLE001
        persist_error = str(exc)[:300]
        logger.exception("clear_emergency_persist_failed")
    return {
        "state": snap.state.value,
        "reason": snap.reason,
        "changed_at": snap.changed_at.isoformat(),
        "alerts_resolved": resolved,
        "persisted": persisted,
        "persist_error": persist_error,
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
    persisted = False
    persist_error = None
    try:
        factory = get_session_factory()
        async with factory() as session:
            await persist_trading_controls(session, trading_controls, changed_by="api")
            if before == "emergency_stop":
                from app.alerts.ops import resolve_alerts_by_code

                n = await resolve_alerts_by_code(
                    session, get_settings(), code="trading.emergency_stop"
                )
                if n:
                    steps.append(f"alerts_resolved:{n}")
            await session.commit()
            persisted = True
    except Exception as exc:  # noqa: BLE001
        persist_error = str(exc)[:300]
        logger.exception("restart_persist_failed")
        steps.append("persist_failed")
    return {
        "state": snap.state.value,
        "reason": snap.reason,
        "changed_at": snap.changed_at.isoformat(),
        "steps": steps,
        "persisted": persisted,
        "persist_error": persist_error,
    }
