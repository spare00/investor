"""Email alert provider stub."""

from __future__ import annotations

from app.alerts.base import AlertProvider, AlertRecord
from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class EmailAlertProvider(AlertProvider):
    """
    Email stub — logs ``email disabled`` unless alerts are enabled and SMTP is configured.

    SMTP is considered configured when ``alert_smtp_host`` is set (optional Phase 7 field).
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _is_active(self) -> bool:
        if not self.settings.enable_alerts:
            return False
        host = getattr(self.settings, "alert_smtp_host", None)
        return bool(host and str(host).strip())

    def send(self, alert: AlertRecord) -> None:
        if not self._is_active():
            logger.info(
                "email disabled",
                alert_code=alert.code,
                alert_severity=alert.severity.value,
                reason="enable_alerts_and_smtp_required",
            )
            return
        logger.info(
            "email_alert_stub",
            alert_code=alert.code,
            alert_severity=alert.severity.value,
            message=alert.message,
        )
