"""Tests for equity tick rounding and alert log levels."""

from __future__ import annotations

from uuid import uuid4

from app.alerts.base import AlertRecord, AlertSeverity
from app.alerts.log_provider import LogAlertProvider
from app.brokers.pricing import round_equity_price


def test_round_equity_price_removes_sub_penny_noise() -> None:
    assert round_equity_price(309.68995) == 309.69
    assert round_equity_price(217.89999999999998) == 217.90
    assert round_equity_price(495.02995) == 495.03
    assert round_equity_price(320.08995000000004) == 320.09
    assert round_equity_price(0.12345) == 0.1235
    assert round_equity_price(None) is None


def test_log_alert_provider_accepts_warning_severity() -> None:
    # Previously raised KeyError: 'WARNING' via structlog LEVEL_TO_NAME.
    LogAlertProvider().send(
        AlertRecord(
            id=uuid4(),
            code="test_warning",
            severity=AlertSeverity.WARNING,
            source="unit",
            message="hello",
        )
    )
    LogAlertProvider().send(
        AlertRecord(
            id=uuid4(),
            code="test_critical",
            severity=AlertSeverity.CRITICAL,
            source="unit",
            message="critical",
        )
    )
