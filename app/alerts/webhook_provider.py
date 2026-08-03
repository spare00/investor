"""Webhook alert provider stub."""

from __future__ import annotations

from app.alerts.base import AlertProvider, AlertRecord
from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class WebhookAlertProvider(AlertProvider):
    """Inactive by default; logs when webhook URL is not configured."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def activate(self) -> None:
        """Test helper to mark provider active."""
        self._active = True

    def send(self, alert: AlertRecord) -> None:
        url = getattr(self.settings, "alert_webhook_url", None)
        if not self.settings.enable_alerts or not url or not self._active:
            logger.debug(
                "webhook_alert_inactive",
                alert_code=alert.code,
                reason="disabled_or_missing_url",
            )
            return
        logger.info(
            "webhook_alert_stub",
            alert_code=alert.code,
            alert_severity=alert.severity.value,
            webhook_url=str(url)[:80],
        )
