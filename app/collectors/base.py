"""Collector DTOs and provider protocols."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(slots=True)
class RawNewsItem:
    headline: str
    source: str
    published_at: datetime
    provider: str
    external_id: str | None = None
    url: str | None = None
    symbols: list[str] = field(default_factory=list)
    category: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RawMarketQuote:
    symbol: str
    as_of: datetime
    provider: str
    last: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    avg_volume_20d: float | None = None
    atr_14: float | None = None
    rsi_14: float | None = None
    sma_20: float | None = None
    sma_50: float | None = None
    sma_200: float | None = None
    bid: float | None = None
    ask: float | None = None
    premarket_change_pct: float | None = None
    gap_pct: float | None = None
    vix: float | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RawMacroSnapshot:
    as_of: datetime
    provider: str
    fed_funds_rate: float | None = None
    cpi_yoy: float | None = None
    pce_yoy: float | None = None
    unemployment_rate: float | None = None
    gdp_growth_q_o_q: float | None = None
    us_10y_yield: float | None = None
    us_2y_yield: float | None = None
    dxy: float | None = None
    wti_oil: float | None = None
    gold: float | None = None
    hy_credit_spread_bps: float | None = None
    notes: list[str] = field(default_factory=list)
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RawEarningsEvent:
    symbol: str
    report_date: datetime
    provider: str
    period: str | None = None
    eps_actual: float | None = None
    eps_estimate: float | None = None
    revenue_actual: float | None = None
    revenue_estimate: float | None = None
    guidance_summary: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RawSecFiling:
    symbol: str
    filed_at: datetime
    form_type: str
    provider: str
    accession: str | None = None
    title: str | None = None
    url: str | None = None
    summary: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)


class NewsProvider(Protocol):
    name: str

    async def fetch_news(
        self,
        *,
        symbols: list[str] | None = None,
        since: datetime | None = None,
        limit: int = 50,
    ) -> list[RawNewsItem]: ...


class MarketDataProvider(Protocol):
    name: str

    async def fetch_quotes(self, symbols: list[str]) -> list[RawMarketQuote]: ...


class MacroDataProvider(Protocol):
    name: str

    async def fetch_macro(self) -> RawMacroSnapshot: ...


class EarningsProvider(Protocol):
    name: str

    async def fetch_earnings(
        self, symbols: list[str], *, since: datetime | None = None
    ) -> list[RawEarningsEvent]: ...


class SecFilingsProvider(Protocol):
    name: str

    async def fetch_filings(
        self, symbols: list[str], *, since: datetime | None = None
    ) -> list[RawSecFiling]: ...
