"""Unit tests for decision price / horizon resolution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.performance.price_lookup import (
    decision_price_max_skew,
    evaluation_horizon_delta,
    evaluation_horizon_label,
    pick_price_at_or_before,
    pick_price_in_window,
    zone_mid,
)


def test_evaluation_horizon_by_book() -> None:
    assert evaluation_horizon_delta("scalp") == timedelta(minutes=240)
    assert evaluation_horizon_delta("day") == timedelta(minutes=390)
    assert evaluation_horizon_delta("short") == timedelta(minutes=10 * 24 * 60)
    assert evaluation_horizon_delta("medium") == timedelta(minutes=60 * 24 * 60)
    assert evaluation_horizon_delta("unknown") == timedelta(minutes=24 * 60)
    assert evaluation_horizon_label("scalp") == "4h"
    assert evaluation_horizon_label("medium") == "60d"
    assert decision_price_max_skew("scalp") == timedelta(hours=6)


def test_pick_price_no_lookahead() -> None:
    t0 = datetime(2026, 8, 1, 14, 0, tzinfo=UTC)
    points = [
        (t0 - timedelta(hours=2), 100.0),
        (t0 - timedelta(minutes=5), 101.0),
        (t0 + timedelta(hours=1), 110.0),  # after decision — must not use as decision price
        (t0 + timedelta(hours=3), 105.0),
        (t0 + timedelta(hours=5), 108.0),  # after 4h scalp horizon — must not use
    ]
    px, ts = pick_price_at_or_before(points, t0, max_skew=timedelta(hours=6))
    assert px == 101.0
    assert ts == t0 - timedelta(minutes=5)

    # Scalp 4h window: only prints through t0+4h
    hp, hts = pick_price_in_window(points, t0, t0 + timedelta(hours=4))
    assert hp == 105.0
    assert hts == t0 + timedelta(hours=3)

    # Skew too tight rejects old decision print
    old_only = [(t0 - timedelta(days=3), 99.0)]
    px2, _ = pick_price_at_or_before(old_only, t0, max_skew=timedelta(hours=6))
    assert px2 is None


def test_zone_mid() -> None:
    assert zone_mid({"min": 100, "max": 102}) == 101.0
    assert zone_mid({"min": 100}) == 100.0
    assert zone_mid(None) is None
