"""Equity chart window and downsample."""

from datetime import UTC, datetime, timedelta

import pytest

from app.performance.equity_chart import chart_window, downsample_equity


def test_chart_window_known_periods() -> None:
    now = datetime(2026, 9, 28, 22, 0, tzinfo=UTC)  # 29 Sep 08:00 Brisbane
    start, end = chart_window("1w", now, timezone_name="Australia/Brisbane")
    assert end == now
    assert start == now - timedelta(days=7)
    ytd, _ = chart_window("ytd", now, timezone_name="Australia/Brisbane")
    assert ytd.year == 2025 and ytd.month == 12 and ytd.day == 31
    all_start, _ = chart_window("all", now, timezone_name="UTC")
    assert all_start.year == 2000


def test_chart_window_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        chart_window("2w", datetime(2026, 1, 1, tzinfo=UTC))


def test_downsample_keeps_ends_and_cap() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    points = [(t0 + timedelta(minutes=i), 100.0 + i) for i in range(1000)]
    out = downsample_equity(points, max_points=240)
    assert len(out) <= 240
    assert out[0] == points[0]
    assert out[-1] == points[-1]
    assert out == sorted(out, key=lambda row: row[0])
