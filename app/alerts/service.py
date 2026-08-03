"""Alert dispatch with cooldown, deduplication, and lifecycle management."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.alerts.base import AlertProvider, AlertRecord, AlertSeverity, AlertStatus
from app.alerts.email_provider import EmailAlertProvider
from app.alerts.fake_provider import FakeAlertProvider
from app.alerts.log_provider import LogAlertProvider
from app.alerts.webhook_provider import WebhookAlertProvider
from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

try:
    from app.models.entities import AlertRecordModel  # type: ignore[attr-defined]
except ImportError:
    AlertRecordModel = None  # type: ignore[misc, assignment]


@dataclass(slots=True)
class EmitResult:
    emitted: bool
    alert_id: UUID | None = None
    reason: str = "ok"
    alert: AlertRecord | None = None


def build_alert_provider(settings: Settings | None = None) -> AlertProvider:
    cfg = settings or get_settings()
    provider = (cfg.alert_provider or "log").lower()
    if provider == "email":
        return EmailAlertProvider(cfg)
    if provider == "webhook":
        return WebhookAlertProvider(cfg)
    if provider == "fake":
        return FakeAlertProvider()
    return LogAlertProvider()


class AlertService:
    """
    Cooldown + dedup alert service.

    Failures are logged and returned as status — callers are not interrupted by default.
    """

    _COOLDOWN_BY_SEVERITY = {
        AlertSeverity.CRITICAL: "critical_alert_cooldown_seconds",
        AlertSeverity.WARNING: "warning_alert_cooldown_seconds",
        AlertSeverity.INFO: None,
    }

    def __init__(
        self,
        session: AsyncSession | None = None,
        settings: Settings | None = None,
        provider: AlertProvider | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.provider = provider or build_alert_provider(self.settings)
        self._alerts: dict[UUID, AlertRecord] = {}
        self._last_emit: dict[str, datetime] = {}

    def _cooldown_seconds(self, severity: AlertSeverity) -> int:
        attr = self._COOLDOWN_BY_SEVERITY.get(severity)
        if attr is None:
            return 0
        return int(getattr(self.settings, attr, 0))

    def _within_cooldown(self, dedupe_key: str, severity: AlertSeverity) -> bool:
        cooldown = self._cooldown_seconds(severity)
        if cooldown <= 0:
            return False
        last = self._last_emit.get(dedupe_key)
        if last is None:
            return False
        return datetime.now(UTC) - last < timedelta(seconds=cooldown)

    async def _persist(self, alert: AlertRecord) -> None:
        if self.session is None or AlertRecordModel is None:
            return
        try:
            row = AlertRecordModel(
                id=alert.id,
                severity=alert.severity.value,
                alert_type=alert.code,
                title=alert.code,
                message=alert.message,
                detected_at=alert.created_at,
                deduplication_key=alert.effective_dedupe_key(),
                status=alert.status.value,
                payload={"source": alert.source, "context": alert.context},
            )
            self.session.add(row)
            await self.session.flush()
        except Exception:
            logger.exception("alert_persist_failed", alert_id=str(alert.id))

    async def emit(
        self,
        *,
        code: str,
        message: str,
        severity: AlertSeverity = AlertSeverity.INFO,
        source: str = "system",
        context: dict[str, Any] | None = None,
        dedupe_key: str | None = None,
    ) -> EmitResult:
        if not self.settings.enable_alerts:
            return EmitResult(emitted=False, reason="alerts_disabled")

        alert = AlertRecord(
            code=code,
            message=message,
            severity=severity,
            source=source,
            context=context or {},
            dedupe_key=dedupe_key,
        )
        key = alert.effective_dedupe_key()

        if self._within_cooldown(key, severity):
            return EmitResult(emitted=False, reason="cooldown", alert_id=alert.id)

        for existing in self._alerts.values():
            if (
                existing.effective_dedupe_key() == key
                and existing.status == AlertStatus.ACTIVE
                and existing.severity == severity
            ):
                return EmitResult(
                    emitted=False,
                    reason="deduplicated",
                    alert_id=existing.id,
                    alert=existing,
                )

        try:
            self.provider.send(alert)
        except Exception:
            logger.exception("alert_provider_send_failed", alert_code=code)
            return EmitResult(emitted=False, reason="provider_error", alert_id=alert.id)

        self._alerts[alert.id] = alert
        self._last_emit[key] = alert.created_at
        try:
            await self._persist(alert)
        except Exception:
            logger.exception("alert_emit_persist_failed", alert_id=str(alert.id))
        return EmitResult(emitted=True, alert_id=alert.id, alert=alert)

    async def acknowledge(self, alert_id: UUID, *, by: str = "operator") -> EmitResult:
        try:
            alert = self._alerts.get(alert_id)
            if alert is None:
                return EmitResult(emitted=False, reason="not_found")
            if alert.status == AlertStatus.RESOLVED:
                return EmitResult(emitted=False, reason="already_resolved", alert_id=alert_id)
            alert.status = AlertStatus.ACKNOWLEDGED
            alert.acknowledged_at = datetime.now(UTC)
            alert.acknowledged_by = by
            return EmitResult(emitted=True, alert_id=alert_id, alert=alert)
        except Exception:
            logger.exception("alert_acknowledge_failed", alert_id=str(alert_id))
            return EmitResult(emitted=False, reason="error")

    async def resolve(self, alert_id: UUID) -> EmitResult:
        try:
            alert = self._alerts.get(alert_id)
            if alert is None:
                return EmitResult(emitted=False, reason="not_found")
            alert.status = AlertStatus.RESOLVED
            alert.resolved_at = datetime.now(UTC)
            return EmitResult(emitted=True, alert_id=alert_id, alert=alert)
        except Exception:
            logger.exception("alert_resolve_failed", alert_id=str(alert_id))
            return EmitResult(emitted=False, reason="error")

    async def list_alerts(
        self,
        *,
        status: AlertStatus | None = None,
        severity: AlertSeverity | None = None,
    ) -> list[AlertRecord]:
        try:
            items = list(self._alerts.values())
            if status is not None:
                items = [a for a in items if a.status == status]
            if severity is not None:
                items = [a for a in items if a.severity == severity]
            return sorted(items, key=lambda a: a.created_at, reverse=True)
        except Exception:
            logger.exception("alert_list_failed")
            return []
