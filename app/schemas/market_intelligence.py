"""Market Intelligence Analyst I/O schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.schemas.common import NewsCategory, Sentiment, StrictModel, TraceMetadata


class NewsItemInput(StrictModel):
    headline: str
    source: str
    published_at: datetime
    url: str | None = None
    symbols: list[str] = Field(default_factory=list)
    raw_text: str | None = None
    provider: str = "unknown"


class MarketIntelligenceInput(StrictModel):
    as_of: datetime
    news_items: list[NewsItemInput] = Field(default_factory=list)
    earnings_summaries: list[dict[str, object]] = Field(default_factory=list)
    analyst_actions: list[dict[str, object]] = Field(default_factory=list)
    sec_filings: list[dict[str, object]] = Field(default_factory=list)
    portfolio_symbols: list[str] = Field(default_factory=list)
    allowlist: list[str] = Field(default_factory=list)
    trace: TraceMetadata = Field(default_factory=TraceMetadata)


class MarketEvent(StrictModel):
    headline: str
    source: str
    published_at: datetime
    symbols: list[str] = Field(default_factory=list)
    category: NewsCategory = NewsCategory.OTHER
    importance: int = Field(ge=1, le=5)
    sentiment: Sentiment = Sentiment.NEUTRAL
    facts: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    interpretation: str | None = None


class MarketIntelligenceOutput(StrictModel):
    timestamp: datetime
    market_events: list[MarketEvent] = Field(default_factory=list)
    top_market_themes: list[str] = Field(default_factory=list)
    data_quality_score: float = Field(ge=0.0, le=1.0)
    conflicts: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    trace: TraceMetadata = Field(default_factory=TraceMetadata)
