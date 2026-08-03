"""Phase 2 unit tests — normalization, universe, stub collectors."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.collectors.base import RawMacroSnapshot, RawMarketQuote, RawNewsItem
from app.collectors.market_data import StubMarketDataProvider
from app.collectors.news import StubNewsProvider
from app.core.config import Settings
from app.services.normalize import (
    aggregate_data_quality,
    headline_hash,
    normalize_macro,
    normalize_market_quote,
    normalize_news_item,
    spread_bps,
)
from app.services.universe import evaluate_symbol_eligibility


@pytest.mark.asyncio
async def test_stub_news_dedupes_on_normalize() -> None:
    items = await StubNewsProvider().fetch_news()
    seen: set[str] = set()
    normalized = [normalize_news_item(i, seen_hashes=seen) for i in items]
    duplicates = [n for n in normalized if n.is_duplicate]
    assert len(items) >= 3
    assert len(duplicates) >= 1
    assert headline_hash(items[0].headline) == headline_hash(
        "Fed officials signal patience on rate cuts"
    )


@pytest.mark.asyncio
async def test_stub_market_quotes_for_allowlist() -> None:
    quotes = await StubMarketDataProvider().fetch_quotes(["SPY", "QQQ", "UNKNOWN"])
    symbols = {q.symbol for q in quotes}
    assert symbols == {"SPY", "QQQ", "UNKNOWN"}
    spy = next(q for q in quotes if q.symbol == "SPY")
    norm = normalize_market_quote(spy)
    assert norm.spread_bps is not None
    assert norm.quality_score > 0.5
    unknown = next(q for q in quotes if q.symbol == "UNKNOWN")
    assert unknown.last > 0


def test_spread_bps_and_macro_quality() -> None:
    assert spread_bps(100.0, 100.2, 100.1) == pytest.approx(19.98001998, rel=1e-3)
    macro = normalize_macro(
        RawMacroSnapshot(
            as_of=datetime.now(UTC),
            provider="stub",
            fed_funds_rate=5.0,
            cpi_yoy=3.0,
            us_10y_yield=4.0,
            dxy=100.0,
            unemployment_rate=4.0,
        )
    )
    assert macro.quality_score >= 0.7


def test_stale_news_lowers_quality() -> None:
    now = datetime.now(UTC)
    stale = RawNewsItem(
        headline="Old headline",
        source="Reuters",
        published_at=now - timedelta(days=2),
        provider="stub",
    )
    fresh = RawNewsItem(
        headline="New headline",
        source="Reuters",
        published_at=now - timedelta(minutes=5),
        provider="stub",
    )
    assert normalize_news_item(stale, now=now).quality_score < normalize_news_item(
        fresh, now=now
    ).quality_score


def test_universe_blocks_penny_and_non_allowlist() -> None:
    settings = Settings(trade_allowlist=["SPY"], penny_stock_max_price=5.0)
    good = normalize_market_quote(
        RawMarketQuote(
            symbol="SPY",
            as_of=datetime.now(UTC),
            provider="stub",
            last=560.0,
            bid=559.9,
            ask=560.1,
            avg_volume_20d=50_000_000,
        )
    )
    penny = normalize_market_quote(
        RawMarketQuote(
            symbol="SPY",
            as_of=datetime.now(UTC),
            provider="stub",
            last=2.0,
            bid=1.9,
            ask=2.1,
            avg_volume_20d=50_000_000,
        )
    )
    other = normalize_market_quote(
        RawMarketQuote(
            symbol="GME",
            as_of=datetime.now(UTC),
            provider="stub",
            last=25.0,
            bid=24.9,
            ask=25.1,
            avg_volume_20d=50_000_000,
        )
    )
    assert evaluate_symbol_eligibility(good, settings=settings).eligible is True
    assert "penny_stock" in evaluate_symbol_eligibility(penny, settings=settings).reasons
    assert "not_in_allowlist" in evaluate_symbol_eligibility(other, settings=settings).reasons


def test_aggregate_quality_fail_closed_threshold() -> None:
    assert aggregate_data_quality([], [], None) == 0.0
    assert aggregate_data_quality([0.9], [0.9], 0.9) >= 0.9
