"""Consecutive listing / focus-streak helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.universe.tenure import (
    churn_summary,
    clear_active_since,
    consecutive_focus_sessions,
    inclusive_calendar_days,
    listed_since,
    stamp_active_since,
)


def test_inclusive_calendar_days_counts_today() -> None:
    now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    start = datetime(2026, 9, 20, 18, 0, tzinfo=UTC)
    assert inclusive_calendar_days(start, now=now) == 4
    assert inclusive_calendar_days(now, now=now) == 1
    assert inclusive_calendar_days(None, now=now) == 0


def test_listed_since_prefers_payload_and_clears_when_inactive() -> None:
    created = datetime(2026, 1, 1, tzinfo=UTC)
    stamped = datetime(2026, 9, 1, tzinfo=UTC)
    payload = {"active_since": stamped.isoformat()}
    assert listed_since(payload, created, active=True) == stamped
    assert listed_since({}, created, active=True) == created
    assert listed_since(payload, created, active=False) is None


def test_stamp_and_clear_active_since() -> None:
    now = datetime(2026, 9, 23, tzinfo=UTC)
    first = stamp_active_since({}, now=now, reset=True)
    assert first["active_since"] == now.isoformat()
    later = datetime(2026, 9, 24, tzinfo=UTC)
    kept = stamp_active_since(first, now=later, reset=False)
    assert kept["active_since"] == now.isoformat()
    paused = clear_active_since(kept, now=later)
    assert "active_since" not in paused
    assert paused["paused_at"] == later.isoformat()
    revived = stamp_active_since(paused, now=later, reset=True)
    assert revived["active_since"] == later.isoformat()


def test_consecutive_focus_sessions_breaks_on_gap() -> None:
    dates = [
        ("2026-09-23", {"SPY", "NVDA"}),
        ("2026-09-22", {"SPY", "NVDA", "ANET"}),
        ("2026-09-21", {"SPY"}),
        ("2026-09-20", {"SPY", "QQQ"}),
    ]
    streaks = consecutive_focus_sessions(dates)
    assert streaks["SPY"] == 4
    assert streaks["NVDA"] == 2
    assert "ANET" not in streaks
    assert "QQQ" not in streaks


def test_churn_summary_flags_stale_book() -> None:
    roster = [
        {"status": "active", "consecutive_listed_days": 40, "in_focus": True},
        {"status": "active", "consecutive_listed_days": 38, "in_focus": True},
        {"status": "active", "consecutive_listed_days": 5, "in_focus": False},
        {"status": "pool", "consecutive_listed_days": 0, "in_focus": False},
    ]
    out = churn_summary(roster, focus_unique_30d=2)
    assert out["active"] == 3
    assert out["stale_listed"] == 2
    assert out["new_listed"] == 1
    assert out["pool_only"] == 1
    assert out["median_listed_days"] == 38
    assert out["focus_unique_30d"] == 2


def test_inclusive_days_accepts_iso_strings() -> None:
    now = datetime(2026, 9, 23, tzinfo=UTC)
    assert inclusive_calendar_days("2026-09-22T00:00:00+00:00", now=now) == 2
    assert inclusive_calendar_days(datetime(2026, 9, 10) + timedelta(days=0), now=now) >= 1
