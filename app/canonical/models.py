"""Canonical data models (Phase 4)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class FreshnessState(StrEnum):
    FRESH = "FRESH"
    ACCEPTABLE = "ACCEPTABLE"
    STALE = "STALE"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class PremarketAvailability(StrEnum):
    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    UNSUPPORTED = "UNSUPPORTED"
    STALE = "STALE"
    MISSING = "MISSING"
    CONFLICTED = "CONFLICTED"


class ConflictState(StrEnum):
    AGREED = "AGREED"
    MINOR_DIFFERENCE = "MINOR_DIFFERENCE"
    MATERIAL_CONFLICT = "MATERIAL_CONFLICT"
    UNRESOLVED = "UNRESOLVED"
    SINGLE_SOURCE_ONLY = "SINGLE_SOURCE_ONLY"


class EconomicEventStatus(StrEnum):
    SCHEDULED = "SCHEDULED"
    RELEASED = "RELEASED"
    REVISED = "REVISED"
    DELAYED = "DELAYED"
    CANCELLED = "CANCELLED"
    MISSING = "MISSING"


class Provenance(BaseModel):
    provider_name: str
    provider_record_id: str | None = None
    raw_payload_reference: str | None = None
    source_timestamp: datetime | None = None
    collection_timestamp: datetime
    normalizer_version: str = "normalize_v1"
    schema_version: str = "canonical_v1"
    transformations_applied: list[str] = Field(default_factory=list)
    validation_result: str = "ok"
    quality_score: float = 1.0


class DataQualityBreakdown(BaseModel):
    overall: float
    freshness: float = 1.0
    completeness: float = 1.0
    source_reliability: float = 1.0
    cross_provider_agreement: float = 1.0
    validation: float = 1.0
    issues: list[str] = Field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


class CanonicalBase(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    schema_version: str = "canonical_v1"
    as_of: datetime
    collected_at: datetime
    source_ids: list[str] = Field(default_factory=list)
    quality: DataQualityBreakdown | None = None
    provenance: Provenance | None = None


class CanonicalQuote(CanonicalBase):
    symbol: str
    asset_class: str = "equity"
    exchange: str | None = None
    currency: str = "USD"
    bid: float | None = None
    ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    last: float
    previous_close: float | None = None
    session: str | None = None
    spread_bps: float | None = None
    freshness: FreshnessState = FreshnessState.UNKNOWN


class CanonicalBar(CanonicalBase):
    symbol: str
    timeframe: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None = None
    session: str | None = None
    freshness: FreshnessState = FreshnessState.UNKNOWN


class CanonicalPremarketSnapshot(CanonicalBase):
    symbol: str
    availability: PremarketAvailability
    premarket_last: float | None = None
    premarket_open: float | None = None
    premarket_high: float | None = None
    premarket_low: float | None = None
    premarket_volume: float | None = None
    gap_from_previous_close_pct: float | None = None
    premarket_spread_bps: float | None = None
    premarket_relative_volume: float | None = None
    premarket_data_start: datetime | None = None
    premarket_data_end: datetime | None = None
    notes: list[str] = Field(default_factory=list)


class CanonicalNewsItem(CanonicalBase):
    news_id: str
    provider_article_id: str | None = None
    headline: str
    summary: str | None = None
    body_excerpt: str | None = None
    source_name: str
    source_url_reference: str | None = None
    author: str | None = None
    published_at: datetime
    updated_at: datetime | None = None
    symbols: list[str] = Field(default_factory=list)
    sectors: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    language: str = "en"
    importance: str = "normal"
    sentiment_hint: str | None = None
    is_press_release: bool = False
    is_opinion: bool = False
    is_rumor: bool = False
    is_correction: bool = False
    source_reliability: float = 0.7


class CanonicalNewsEventCluster(CanonicalBase):
    event_cluster_id: str
    primary_article_id: str
    member_article_ids: list[str] = Field(default_factory=list)
    first_seen_at: datetime
    last_updated_at: datetime
    affected_symbols: list[str] = Field(default_factory=list)
    category: str | None = None
    confidence: float = 1.0
    deduplication_method: str = "provider_id"


class CanonicalSecFiling(CanonicalBase):
    filing_id: str
    accession_number: str
    form_type: str
    company_name: str
    cik: str
    symbols: list[str] = Field(default_factory=list)
    filed_at: datetime
    period_of_report: datetime | None = None
    document_url_reference: str | None = None
    primary_document: str | None = None
    items: list[str] = Field(default_factory=list)
    is_amendment: bool = False
    importance_hints: list[str] = Field(default_factory=list)


class CanonicalEconomicEvent(CanonicalBase):
    event_id: str
    event_name: str
    country: str = "US"
    scheduled_at: datetime
    released_at: datetime | None = None
    importance: str = "medium"
    actual: float | None = None
    consensus: float | None = None
    previous: float | None = None
    revised_previous: float | None = None
    unit: str | None = None
    status: EconomicEventStatus = EconomicEventStatus.SCHEDULED
    surprise_value: float | None = None
    surprise_direction: str | None = None


class CanonicalMarketSnapshot(CanonicalBase):
    symbol: str
    quote: CanonicalQuote | None = None
    daily_bar: CanonicalBar | None = None
    premarket: CanonicalPremarketSnapshot | None = None
    indicators: dict[str, float | None] = Field(default_factory=dict)
    calculation_version: str | None = None
    input_snapshot_id: str | None = None


class CanonicalProviderStatus(BaseModel):
    provider_name: str
    healthy: bool
    status: str
    last_success_at: datetime | None = None
    last_error: str | None = None
    capabilities: dict[str, Any] = Field(default_factory=dict)


class CanonicalDataConflict(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    data_type: str
    symbol_or_key: str
    state: ConflictState
    primary_value: Any = None
    secondary_value: Any = None
    difference: float | None = None
    tolerance: float | None = None
    provider_names: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
