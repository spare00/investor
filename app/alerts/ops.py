"""Operational alert emitters (recon, emergency stop, LLM budget)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.alerts.base import AlertSeverity
from app.alerts.service import AlertService, EmitResult
from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_RECON_ALERT_RESULTS = frozenset(
    {"MATERIAL_DRIFT", "BROKER_UNAVAILABLE", "LOCAL_STATE_INVALID"}
)


async def emit_reconciliation_alert(
    session: AsyncSession | None,
    settings: Settings | None = None,
    *,
    result: str,
    issues: list[Any] | None = None,
    sync_type: str = "",
) -> EmitResult | None:
    """CRITICAL when recon cannot trust broker/local books."""
    if result not in _RECON_ALERT_RESULTS:
        return None
    cfg = settings or get_settings()
    severity = (
        AlertSeverity.CRITICAL
        if result in {"MATERIAL_DRIFT", "LOCAL_STATE_INVALID"}
        else AlertSeverity.WARNING
    )
    try:
        return await AlertService(session, settings=cfg).emit(
            code=f"recon.{result.lower()}",
            message=f"Broker reconciliation {result}"
            + (f" ({sync_type})" if sync_type else ""),
            severity=severity,
            source="reconciliation",
            context={"result": result, "sync_type": sync_type, "issues": (issues or [])[:20]},
            dedupe_key=f"recon:{result}",
        )
    except Exception:  # noqa: BLE001
        logger.exception("ops_alert_recon_failed", result=result)
        return None


async def emit_emergency_stop_alert(
    session: AsyncSession | None,
    settings: Settings | None = None,
    *,
    reason: str,
    source: str = "api",
) -> EmitResult | None:
    cfg = settings or get_settings()
    try:
        return await AlertService(session, settings=cfg).emit(
            code="trading.emergency_stop",
            message=f"Emergency stop engaged: {reason}",
            severity=AlertSeverity.CRITICAL,
            source=source,
            context={"reason": reason},
            dedupe_key="trading:emergency_stop",
        )
    except Exception:  # noqa: BLE001
        logger.exception("ops_alert_emergency_failed")
        return None


async def emit_llm_budget_alert(
    session: AsyncSession | None = None,
    settings: Settings | None = None,
    *,
    code: str,
    message: str,
    severity: AlertSeverity = AlertSeverity.WARNING,
    context: dict[str, Any] | None = None,
) -> EmitResult | None:
    cfg = settings or get_settings()
    try:
        return await AlertService(session, settings=cfg).emit(
            code=code,
            message=message,
            severity=severity,
            source="llm_budget",
            context=context or {},
            dedupe_key=code,
        )
    except Exception:  # noqa: BLE001
        logger.exception("ops_alert_llm_budget_failed", code=code)
        return None


async def emit_hard_stop_alert(
    session: AsyncSession | None,
    settings: Settings | None = None,
    *,
    symbol: str,
    price: float | None = None,
    stop_price: float | None = None,
    submitted: bool = False,
    intent_id: str | None = None,
) -> EmitResult | None:
    """CRITICAL when a hard stop fires for a symbol (deduped per symbol/day)."""
    from datetime import UTC, datetime

    cfg = settings or get_settings()
    day = datetime.now(UTC).date().isoformat()
    sym = symbol.upper()
    try:
        return await AlertService(session, settings=cfg).emit(
            code="trading.hard_stop",
            message=f"Hard stop triggered on {sym}"
            + (" (orders submitted)" if submitted else " (intent pending)"),
            severity=AlertSeverity.CRITICAL,
            source="position_monitor",
            context={
                "symbol": sym,
                "price": price,
                "stop_price": stop_price,
                "submitted": submitted,
                "intent_id": intent_id,
            },
            dedupe_key=f"hard_stop:{sym}:{day}",
        )
    except Exception:  # noqa: BLE001
        logger.exception("ops_alert_hard_stop_failed", symbol=sym)
        return None


async def emit_monitor_emergency_alert(
    session: AsyncSession | None,
    settings: Settings | None = None,
    *,
    symbol: str,
    reasons: list[str] | None = None,
) -> EmitResult | None:
    """CRITICAL when monitor verdict is EMERGENCY_ACTION_REQUIRED."""
    from datetime import UTC, datetime

    cfg = settings or get_settings()
    day = datetime.now(UTC).date().isoformat()
    sym = symbol.upper()
    why = ", ".join(reasons or []) or "emergency"
    try:
        return await AlertService(session, settings=cfg).emit(
            code="trading.monitor_emergency",
            message=f"Monitor emergency on {sym}: {why}",
            severity=AlertSeverity.CRITICAL,
            source="position_monitor",
            context={"symbol": sym, "reasons": reasons or []},
            dedupe_key=f"monitor_emergency:{sym}:{day}",
        )
    except Exception:  # noqa: BLE001
        logger.exception("ops_alert_monitor_emergency_failed", symbol=sym)
        return None


async def emit_overnight_review_alert(
    session: AsyncSession | None,
    settings: Settings | None = None,
    *,
    reviews: list[dict[str, Any]],
    session_date: str = "",
) -> EmitResult | None:
    """WARNING when overnight review flags leftovers or manual review."""
    flagged = [
        r
        for r in reviews
        if str(r.get("status") or "")
        in {"MANUAL_REVIEW_REQUIRED", "CLOSE_BEFORE_MARKET_CLOSE", "OVERNIGHT_APPROVED_WITH_REDUCTION"}
    ]
    if not flagged:
        return None
    cfg = settings or get_settings()
    critical = any(r.get("status") == "MANUAL_REVIEW_REQUIRED" for r in flagged)
    try:
        return await AlertService(session, settings=cfg).emit(
            code="trading.overnight_review",
            message=f"Overnight review: {len(flagged)} position(s) need attention"
            + (f" ({session_date})" if session_date else ""),
            severity=AlertSeverity.CRITICAL if critical else AlertSeverity.WARNING,
            source="overnight_review",
            context={"session_date": session_date, "flagged": flagged[:20]},
            dedupe_key=f"overnight_review:{session_date or 'today'}",
        )
    except Exception:  # noqa: BLE001
        logger.exception("ops_alert_overnight_failed")
        return None


async def resolve_alerts_by_code(
    session: AsyncSession | None,
    settings: Settings | None = None,
    *,
    code: str,
) -> int:
    """Resolve active alerts matching ``code`` (in-memory + DB)."""
    cfg = settings or get_settings()
    try:
        return await AlertService(session, settings=cfg).resolve_by_code(code)
    except Exception:  # noqa: BLE001
        logger.exception("ops_alert_resolve_by_code_failed", code=code)
        return 0
