"""Normalize raw collector payloads into persistence-ready records."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.collectors.base import RawMacroSnapshot, RawMarketQuote, RawNewsItem
from app.services.data_quality import normalize_headline, score_freshness


@dataclass(slots=True)
class NormalizedNews:
    provider: str
    external_id: str | None
    headline: str
    headline_hash: str
    source: str
    url: str | None
    published_at: datetime
    collected_at: datetime
    symbols: list[str]
    category: str | None
    raw_payload: dict[str, Any]
    freshness_score: float
    quality_score: float
    is_duplicate: bool = False


@dataclass(slots=True)
class NormalizedMarketSnapshot:
    symbol: str
    as_of: datetime
    provider: str
    last: float
    open: float | None
    high: float | None
    low: float | None
    volume: float | None
    avg_volume_20d: float | None
    atr_14: float | None
    rsi_14: float | None
    sma_20: float | None
    sma_50: float | None
    sma_200: float | None
    bid: float | None
    ask: float | None
    spread_bps: float | None
    premarket_change_pct: float | None
    gap_pct: float | None
    vix: float | None
    raw_payload: dict[str, Any]
    freshness_score: float
    quality_score: float


@dataclass(slots=True)
class NormalizedMacroSnapshot:
    as_of: datetime
    provider: str
    fed_funds_rate: float | None
    cpi_yoy: float | None
    pce_yoy: float | None
    unemployment_rate: float | None
    gdp_growth_q_o_q: float | None
    us_10y_yield: float | None
    us_2y_yield: float | None
    dxy: float | None
    wti_oil: float | None
    gold: float | None
    hy_credit_spread_bps: float | None
    notes: list[str]
    raw_payload: dict[str, Any]
    freshness_score: float
    quality_score: float


def headline_hash(headline: str) -> str:
    return hashlib.sha256(normalize_headline(headline).encode("utf-8")).hexdigest()


def spread_bps(bid: float | None, ask: float | None, last: float) -> float | None:
    if bid is None or ask is None or last <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None
    return (ask - bid) / mid * 10_000.0


def score_news_quality(item: RawNewsItem, *, now: datetime | None = None) -> float:
    current = now or datetime.now(UTC)
    score = 1.0
    if not item.source.strip():
        score -= 0.5
    if not item.headline.strip():
        score -= 0.5
    if item.published_at.tzinfo is None:
        score -= 0.1
    age_score = score_freshness(item.published_at, now=current, max_age_minutes=360)
    score = min(score, 0.4 + 0.6 * age_score)
    return max(0.0, min(1.0, round(score, 4)))


def score_market_quality(quote: RawMarketQuote, *, now: datetime | None = None) -> float:
    current = now or datetime.now(UTC)
    score = 1.0
    required = [quote.last, quote.as_of]
    if any(v is None for v in required) or quote.last <= 0:
        return 0.0
    if quote.bid is None or quote.ask is None:
        score -= 0.15
    if quote.avg_volume_20d is None:
        score -= 0.1
    if quote.atr_14 is None:
        score -= 0.1
    age = score_freshness(quote.as_of, now=current, max_age_minutes=30)
    score = min(score, 0.3 + 0.7 * age)
    return max(0.0, min(1.0, round(score, 4)))


def score_macro_quality(macro: RawMacroSnapshot, *, now: datetime | None = None) -> float:
    current = now or datetime.now(UTC)
    fields = [
        macro.fed_funds_rate,
        macro.cpi_yoy,
        macro.us_10y_yield,
        macro.dxy,
        macro.unemployment_rate,
    ]
    present = sum(1 for f in fields if f is not None)
    completeness = present / len(fields)
    age = score_freshness(macro.as_of, now=current, max_age_minutes=24 * 60)
    return round(max(0.0, min(1.0, 0.5 * completeness + 0.5 * age)), 4)


def normalize_news_item(
    item: RawNewsItem,
    *,
    collected_at: datetime | None = None,
    now: datetime | None = None,
    seen_hashes: set[str] | None = None,
) -> NormalizedNews:
    current = now or datetime.now(UTC)
    collected = collected_at or current
    h = headline_hash(item.headline)
    duplicate = bool(seen_hashes is not None and h in seen_hashes)
    if seen_hashes is not None:
        seen_hashes.add(h)
    quality = score_news_quality(item, now=current)
    if duplicate:
        quality = min(quality, 0.2)
    return NormalizedNews(
        provider=item.provider,
        external_id=item.external_id,
        headline=item.headline.strip(),
        headline_hash=h,
        source=item.source.strip(),
        url=item.url,
        published_at=item.published_at if item.published_at.tzinfo else item.published_at.replace(tzinfo=UTC),
        collected_at=collected,
        symbols=[s.upper() for s in item.symbols],
        category=item.category,
        raw_payload=item.raw_payload,
        freshness_score=score_freshness(item.published_at, now=current, max_age_minutes=360),
        quality_score=quality,
        is_duplicate=duplicate,
    )


def normalize_market_quote(
    quote: RawMarketQuote, *, now: datetime | None = None
) -> NormalizedMarketSnapshot:
    current = now or datetime.now(UTC)
    as_of = quote.as_of if quote.as_of.tzinfo else quote.as_of.replace(tzinfo=UTC)
    return NormalizedMarketSnapshot(
        symbol=quote.symbol.upper(),
        as_of=as_of,
        provider=quote.provider,
        last=quote.last,
        open=quote.open,
        high=quote.high,
        low=quote.low,
        volume=quote.volume,
        avg_volume_20d=quote.avg_volume_20d,
        atr_14=quote.atr_14,
        rsi_14=quote.rsi_14,
        sma_20=quote.sma_20,
        sma_50=quote.sma_50,
        sma_200=quote.sma_200,
        bid=quote.bid,
        ask=quote.ask,
        spread_bps=spread_bps(quote.bid, quote.ask, quote.last),
        premarket_change_pct=quote.premarket_change_pct,
        gap_pct=quote.gap_pct,
        vix=quote.vix,
        raw_payload=quote.raw_payload,
        freshness_score=score_freshness(as_of, now=current, max_age_minutes=30),
        quality_score=score_market_quality(quote, now=current),
    )


def normalize_macro(
    macro: RawMacroSnapshot, *, now: datetime | None = None
) -> NormalizedMacroSnapshot:
    current = now or datetime.now(UTC)
    as_of = macro.as_of if macro.as_of.tzinfo else macro.as_of.replace(tzinfo=UTC)
    return NormalizedMacroSnapshot(
        as_of=as_of,
        provider=macro.provider,
        fed_funds_rate=macro.fed_funds_rate,
        cpi_yoy=macro.cpi_yoy,
        pce_yoy=macro.pce_yoy,
        unemployment_rate=macro.unemployment_rate,
        gdp_growth_q_o_q=macro.gdp_growth_q_o_q,
        us_10y_yield=macro.us_10y_yield,
        us_2y_yield=macro.us_2y_yield,
        dxy=macro.dxy,
        wti_oil=macro.wti_oil,
        gold=macro.gold,
        hy_credit_spread_bps=macro.hy_credit_spread_bps,
        notes=list(macro.notes),
        raw_payload=macro.raw_payload,
        freshness_score=score_freshness(as_of, now=current, max_age_minutes=24 * 60),
        quality_score=score_macro_quality(macro, now=current),
    )


def aggregate_data_quality(
    news_scores: list[float],
    market_scores: list[float],
    macro_score: float | None,
) -> float:
    parts: list[float] = []
    if news_scores:
        parts.append(sum(news_scores) / len(news_scores))
    if market_scores:
        parts.append(sum(market_scores) / len(market_scores))
    if macro_score is not None:
        parts.append(macro_score)
    if not parts:
        return 0.0
    return round(sum(parts) / len(parts), 4)
