"""Alert domain types and provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


class AlertSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertStatus(StrEnum):
    ACTIVE = "active"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    SUPPRESSED = "suppressed"


@dataclass(slots=True)
class AlertRecord:
    """In-memory alert record; optional DB mirror when a model exists."""

    code: str
    message: str
    severity: AlertSeverity
    source: str = "system"
    context: dict[str, Any] = field(default_factory=dict)
    dedupe_key: str | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    status: AlertStatus = AlertStatus.ACTIVE
    acknowledged_at: datetime | None = None
    acknowledged_by: str | None = None
    resolved_at: datetime | None = None

    def effective_dedupe_key(self) -> str:
        return self.dedupe_key or f"{self.code}:{self.severity.value}"


class AlertProvider(ABC):
    """Deliver an alert to an external channel."""

    @abstractmethod
    def send(self, alert: AlertRecord) -> None:
        """Send alert; implementations should not raise to callers."""
