"""Venue registry and ASX calendar tests."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.core.config import clear_settings_cache, get_settings
from app.market.calendar import MarketCalendarService
from app.market.venues import Venue, ib_qualify_candidates, resolve_venue


SYD = ZoneInfo("Australia/Sydney")
ET = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRIMARY_VENUE", "US")
    monkeypatch.setenv("MARKET_TIMEZONE", "America/New_York")
    monkeypatch.setenv("OPERATOR_TIMEZONE", "Australia/Brisbane")
    clear_settings_cache()
    yield
    clear_settings_cache()


def test_resolve_primary_venue() -> None:
    assert resolve_venue(get_settings()) == Venue.US
    assert resolve_venue(get_settings(), venue="AU") == Venue.AU


def test_asx_regular_session() -> None:
    cal = MarketCalendarService(get_settings(), venue=Venue.AU)
    day = date(2026, 8, 10)  # Monday
    assert cal.is_trading_day(day)
    session = cal.get_session(day)
    assert session.venue == "AU"
    assert session.calendar_source == "XASX"
    open_local = session.regular_open.astimezone(SYD)
    close_local = session.regular_close.astimezone(SYD)
    assert open_local.hour == 10 and open_local.minute == 0
    assert close_local.hour == 16 and close_local.minute == 0


def test_asx_weekend() -> None:
    cal = MarketCalendarService(get_settings(), venue="AU")
    assert not cal.is_trading_day(date(2026, 8, 8))  # Saturday
    assert not cal.is_trading_day(date(2026, 8, 9))  # Sunday


def test_asx_australia_day_holiday() -> None:
    cal = MarketCalendarService(get_settings(), venue=Venue.AU)
    # Australia Day 2026 falls on Monday 26 Jan
    assert not cal.is_trading_day(date(2026, 1, 26))


def test_asx_status_during_rth() -> None:
    cal = MarketCalendarService(get_settings(), venue=Venue.AU)
    now = datetime(2026, 8, 10, 11, 0, tzinfo=SYD)
    status = cal.get_market_status(now)
    assert status.venue == "AU"
    assert status.phase == "REGULAR"
    assert status.as_of_market_local is not None


def test_us_calendar_unchanged_default() -> None:
    cal = MarketCalendarService(get_settings())
    session = cal.get_session(date(2026, 8, 3))
    assert session.venue == "US"
    assert session.regular_open.astimezone(ET).hour == 9


def test_ib_qualify_candidates_prefer_au() -> None:
    pairs = ib_qualify_candidates(get_settings(), venue=Venue.AU)
    assert pairs[0] == ("SMART", "AUD")
    assert ("ASX", "AUD") in pairs
    assert ("SMART", "USD") in pairs


def test_venue_for_symbol_allowlist_and_exchange() -> None:
    from app.market.venues import venue_for_symbol

    settings = get_settings()
    assert venue_for_symbol("JPEQ", settings).value == "AU"
    assert venue_for_symbol("AAPL", settings).value == "US"
    assert venue_for_symbol("XYZ", settings, exchange="ASX", currency="AUD").value == "AU"
    assert venue_for_symbol("XYZ", settings, venue="AU").value == "AU"


def test_summarize_venue_books() -> None:
    from app.market.books import summarize_venue_books

    books = summarize_venue_books(
        [
            {"symbol": "AAPL", "quantity": 1, "market_value": 100, "venue": "US", "currency": "USD"},
            {"symbol": "JPEQ", "quantity": 10, "market_value": 600, "exchange": "ASX", "currency": "AUD"},
        ],
        settings=get_settings(),
        equity=1000,
    )
    assert books["US"]["positions"] == 1
    assert books["AU"]["positions"] == 1
    assert books["AU"]["market_value"] == 600


def test_collection_universe_au_uses_vas_not_spy(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.database import Base
    from app.universe.service import UniverseService

    monkeypatch.setenv("ENABLED_VENUES", "US,AU")
    monkeypatch.setenv("UNIVERSE_MODE", "static")
    clear_settings_cache()

    async def _run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            univ = UniverseService(session, settings=get_settings())
            symbols = await univ.collection_universe(venue="AU")
            assert "VAS" in symbols
            assert "SPY" not in symbols
            assert "BHP" in symbols or "JPEQ" in symbols
        await engine.dispose()

    asyncio.run(_run())


def test_dual_venue_prepare_distinct_job_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.database import Base
    from app.execution.safety_controls import trading_controls
    from app.workflow.daily import DailyWorkflowService

    trading_controls.clear_emergency()
    if trading_controls.snapshot().state.value != "active":
        trading_controls.resume("test_reset")
    monkeypatch.setenv("ENABLED_VENUES", "US,AU")
    clear_settings_cache()
    settings = get_settings()

    async def _run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            us = DailyWorkflowService(session, settings=settings, venue="US")
            au = DailyWorkflowService(session, settings=settings, venue="AU")
            day = "2026-08-10"
            await us.prepare(session_date=day)
            await au.prepare(session_date=day)
            all_jobs = await us.planned_jobs(session_date=day)
            us_jobs = {j["job_key"] for j in all_jobs if j["job_key"].startswith("US:")}
            au_jobs = {j["job_key"] for j in all_jobs if j["job_key"].startswith("AU:")}
            assert "US:premarket_analysis" in us_jobs
            assert "AU:premarket_analysis" in au_jobs
            assert us_jobs.isdisjoint(au_jobs)
            assert (await us.get_current(day)).calendar_name == "NYSE"
            assert (await au.get_current(day)).calendar_name == "ASX"
        await engine.dispose()

    asyncio.run(_run())


def test_holdings_for_venue_filters_books(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.market.venues import holdings_for_venue

    monkeypatch.setenv("ENABLED_VENUES", "US,AU")
    monkeypatch.setenv("TRADE_ALLOWLIST_AU", "BHP,VAS")
    clear_settings_cache()
    positions = [
        {"symbol": "AAPL", "quantity": 10, "venue": "US", "currency": "USD"},
        {"symbol": "BHP", "quantity": 5, "venue": "AU", "currency": "AUD"},
        {"symbol": "CBA", "quantity": 0, "venue": "AU", "currency": "AUD"},
    ]
    assert holdings_for_venue(positions, "AU", get_settings()) == ["BHP"]
    assert holdings_for_venue(positions, "US", get_settings()) == ["AAPL"]


def test_news_relevant_to_venue(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.market.venues import news_relevant_to_venue

    monkeypatch.setenv("ENABLED_VENUES", "US,AU")
    monkeypatch.setenv("TRADE_ALLOWLIST_AU", "BHP,VAS")
    clear_settings_cache()
    cfg = get_settings()
    assert news_relevant_to_venue([], "AU", settings=cfg) is True  # macro
    assert news_relevant_to_venue(["BHP"], "AU", settings=cfg) is True
    assert news_relevant_to_venue(["AAPL"], "AU", settings=cfg) is False
    assert news_relevant_to_venue(["AAPL"], "AU", settings=cfg, held_symbols={"AAPL"}) is True
    assert news_relevant_to_venue(["AAPL"], "US", settings=cfg) is True
