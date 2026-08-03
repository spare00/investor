"""In-memory alert provider for tests."""

from __future__ import annotations

from app.alerts.base import AlertProvider, AlertRecord


class FakeAlertProvider(AlertProvider):
    """Collect sent alerts in a list for assertions."""

    def __init__(self) -> None:
        self.sent: list[AlertRecord] = []

    def send(self, alert: AlertRecord) -> None:
        self.sent.append(alert)

    def clear(self) -> None:
        self.sent.clear()
