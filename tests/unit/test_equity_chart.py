"""Equity chart window and downsample."""

from datetime import UTC, datetime, timedelta

import pytest

from app.performance.equity_chart import (
    chart_window,
    downsample_equity,
    prices_at_marks,
    rebase_prices_to_equity,
)


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


def test_prices_at_marks_keep_the_real_quote() -> None:
    t0 = datetime(2026, 9, 16, tzinfo=UTC)
    t1 = t0 + timedelta(days=1)
    equity = [(t0, 978_000.0), (t1, 980_000.0)]
    prices = [(t0, 280.0), (t1, 274.0)]
    assert prices_at_marks(equity, prices) == pytest.approx([280.0, 274.0])


def test_rebase_grows_starting_equity_with_the_index() -> None:
    t0 = datetime(2026, 9, 16, tzinfo=UTC)
    t1 = t0 + timedelta(days=1)
    equity = [(t0, 1000.0), (t1, 1100.0)]
    prices = [(t0 - timedelta(hours=2), 200.0), (t0, 200.0), (t1, 220.0)]
    out = rebase_prices_to_equity(equity, prices)
    assert out[0] == pytest.approx(1000.0)
    assert out[1] == pytest.approx(1100.0)


def test_rebase_waits_until_the_first_price() -> None:
    t0 = datetime(2026, 9, 1, tzinfo=UTC)
    t1 = t0 + timedelta(days=2)
    equity = [(t0, 500.0), (t1, 500.0)]
    prices = [(t1, 50.0), (t1 + timedelta(days=1), 60.0)]
    out = rebase_prices_to_equity(equity, prices)
    assert out[0] is None
    assert out[1] == pytest.approx(500.0)


def test_downsample_keeps_ends_and_cap() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    points = [(t0 + timedelta(minutes=i), 100.0 + i) for i in range(1000)]
    out = downsample_equity(points, max_points=240)
    assert len(out) <= 240
    assert out[0] == points[0]
    assert out[-1] == points[-1]
    assert out == sorted(out, key=lambda row: row[0])
