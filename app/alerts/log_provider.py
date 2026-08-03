"""Log-based alert provider."""

from __future__ import annotations

from app.alerts.base import AlertProvider, AlertRecord
from app.core.logging import get_logger

logger = get_logger(__name__)


class LogAlertProvider(AlertProvider):
    """Emit alerts through structured logging."""

    def send(self, alert: AlertRecord) -> None:
        logger.bind(
            alert_id=str(alert.id),
            alert_code=alert.code,
            alert_severity=alert.severity.value,
            alert_source=alert.source,
            alert_context=alert.context,
        ).log(
            alert.severity.value.upper(),
            "alert_emitted",
            message=alert.message,
        )
