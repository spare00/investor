"""Phase 3 market calendar tests."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.core.config import clear_settings_cache, get_settings
from app.market.calendar import MarketCalendarService

ET = ZoneInfo("America/New_York")
BNE = ZoneInfo("Australia/Brisbane")


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARKET_TIMEZONE", "America/New_York")
    monkeypatch.setenv("OPERATOR_TIMEZONE", "Australia/Brisbane")
    clear_settings_cache()
    yield
    clear_settings_cache()


def test_regular_trading_day() -> None:
    cal = MarketCalendarService(get_settings())
    # Monday 2026-08-03
    day = date(2026, 8, 3)
    assert cal.is_trading_day(day)
    session = cal.get_session(day)
    assert session.is_trading_day
    assert not session.is_early_close
    assert session.regular_open is not None
    assert session.regular_open.astimezone(ET).hour == 9
    assert session.regular_close.astimezone(ET).hour == 16


def test_weekend_non_trading() -> None:
    cal = MarketCalendarService(get_settings())
    assert not cal.is_trading_day(date(2026, 8, 1))  # Saturday
    assert not cal.is_trading_day(date(2026, 8, 2))  # Sunday


def test_us_holiday() -> None:
    cal = MarketCalendarService(get_settings())
    # Thanksgiving 2026-11-26
    assert not cal.is_trading_day(date(2026, 11, 26))


def test_early_close() -> None:
    cal = MarketCalendarService(get_settings())
    # Day after Thanksgiving 2026-11-27 typically early close
    session = cal.get_session(date(2026, 11, 27))
    assert session.is_trading_day
    assert session.is_early_close
    assert session.regular_close is not None
    assert session.regular_close.astimezone(ET).hour == 13


def test_next_previous_trading_day() -> None:
    cal = MarketCalendarService(get_settings())
    assert cal.get_next_trading_day(date(2026, 8, 1)) == date(2026, 8, 3)
    assert cal.get_previous_trading_day(date(2026, 8, 3)) == date(2026, 7, 31)


def test_dst_spring_forward_open_still_et() -> None:
    cal = MarketCalendarService(get_settings())
    # First Monday after US DST start 2026-03-08
    session = cal.get_session(date(2026, 3, 9))
    assert session.is_trading_day
    open_et = session.regular_open.astimezone(ET)
    assert open_et.hour == 9 and open_et.minute == 30
    # UTC offset should be -4 (EDT)
    assert open_et.utcoffset().total_seconds() == -4 * 3600


def test_dst_fall_back_open_still_et() -> None:
    cal = MarketCalendarService(get_settings())
    # First Monday after US DST end 2026-11-01
    session = cal.get_session(date(2026, 11, 2))
    assert session.is_trading_day
    open_et = session.regular_open.astimezone(ET)
    assert open_et.hour == 9 and open_et.minute == 30
    assert open_et.utcoffset().total_seconds() == -5 * 3600


def test_ny_brisbane_conversion() -> None:
    cal = MarketCalendarService(get_settings())
    # Winter EST: 09:30 ET = 00:30 next day BNE (UTC+10)
    now = datetime(2026, 1, 5, 9, 30, tzinfo=ET)
    status = cal.get_market_status(now)
    bne = datetime.fromisoformat(status.as_of_brisbane)
    assert bne.tzinfo is not None
    assert bne.astimezone(BNE).hour == 0
    # Summer EDT: 09:30 ET = 23:30 same day BNE
    now2 = datetime(2026, 7, 6, 9, 30, tzinfo=ET)
    status2 = cal.get_market_status(now2)
    bne2 = datetime.fromisoformat(status2.as_of_brisbane).astimezone(BNE)
    assert bne2.hour == 23


def test_naive_datetime_normalized() -> None:
    cal = MarketCalendarService(get_settings())
    naive = datetime(2026, 8, 3, 14, 0)  # treated as UTC by service
    status = cal.get_market_status(naive)
    assert status.as_of.tzinfo is not None


def test_after_hours_reports_next_open() -> None:
    cal = MarketCalendarService(get_settings())
    # Tuesday evening ET after regular close
    now = datetime(2026, 8, 4, 20, 0, tzinfo=ET)
    status = cal.get_market_status(now)
    assert status.phase == "AFTER_HOURS"
    assert status.next_open is not None
    assert status.minutes_to_next_open is not None
    assert status.minutes_to_next_open > 0
    assert status.next_open.astimezone(ET).hour == 9


def test_schedule_range_includes_weekend() -> None:
    cal = MarketCalendarService(get_settings())
    rows = cal.get_schedule(date(2026, 7, 31), date(2026, 8, 3))
    assert len(rows) == 4
    assert not rows[1].is_trading_day  # Saturday Aug 1
