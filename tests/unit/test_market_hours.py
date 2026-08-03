"""Unit tests for market session helpers."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.services.market_hours import is_regular_session, is_weekday, minutes_to_close

ET = ZoneInfo("America/New_York")


def test_weekday_and_weekend() -> None:
    monday = datetime(2026, 8, 3, 10, 0, tzinfo=ET)  # Monday
    saturday = datetime(2026, 8, 8, 10, 0, tzinfo=ET)
    assert is_weekday(monday) is True
    assert is_weekday(saturday) is False
    assert is_regular_session(saturday) is False


def test_regular_session_bounds() -> None:
    before = datetime(2026, 8, 3, 9, 29, tzinfo=ET)
    open_ = datetime(2026, 8, 3, 9, 30, tzinfo=ET)
    mid = datetime(2026, 8, 3, 12, 0, tzinfo=ET)
    close = datetime(2026, 8, 3, 16, 0, tzinfo=ET)
    assert is_regular_session(before) is False
    assert is_regular_session(open_) is True
    assert is_regular_session(mid) is True
    assert is_regular_session(close) is False


def test_minutes_to_close() -> None:
    mid = datetime(2026, 8, 3, 15, 45, tzinfo=ET)
    assert minutes_to_close(mid) == 15
    assert minutes_to_close(datetime(2026, 8, 3, 8, 0, tzinfo=ET)) is None
