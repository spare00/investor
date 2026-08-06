"""Log-based alert provider."""

from __future__ import annotations

import logging

from app.alerts.base import AlertProvider, AlertRecord, AlertSeverity
from app.core.logging import get_logger

logger = get_logger(__name__)

_LEVEL = {
    AlertSeverity.INFO: logging.INFO,
    AlertSeverity.WARNING: logging.WARNING,
    AlertSeverity.CRITICAL: logging.CRITICAL,
}


class LogAlertProvider(AlertProvider):
    """Emit alerts through structured logging."""

    def send(self, alert: AlertRecord) -> None:
        level = _LEVEL.get(alert.severity, logging.INFO)
        logger.bind(
            alert_id=str(alert.id),
            alert_code=alert.code,
            alert_severity=alert.severity.value,
            alert_source=alert.source,
            alert_context=alert.context,
        ).log(
            level,
            "alert_emitted",
            message=alert.message,
        )
