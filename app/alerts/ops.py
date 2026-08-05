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
