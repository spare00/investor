"""Macro & Policy Strategist I/O schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.schemas.common import MarketRegime, StrictModel, TraceMetadata


class MacroSnapshotInput(StrictModel):
    as_of: datetime
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
    notes: list[str] = Field(default_factory=list)


class MacroStrategistInput(StrictModel):
    as_of: datetime
    macro: MacroSnapshotInput
    geopolitical_events: list[str] = Field(default_factory=list)
    market_intelligence_summary: dict[str, object] | None = None
    trace: TraceMetadata = Field(default_factory=TraceMetadata)


class SectorImpact(StrictModel):
    sector: str
    bias: str  # bullish | bearish | neutral
    rationale: str


class MacroStrategistOutput(StrictModel):
    timestamp: datetime
    market_regime: MarketRegime
    confidence: float = Field(ge=0.0, le=1.0)
    bullish_factors: list[str] = Field(default_factory=list)
    bearish_factors: list[str] = Field(default_factory=list)
    expected_sector_impact: list[SectorImpact] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    data_quality_score: float = Field(ge=0.0, le=1.0)
    conflicts: list[str] = Field(default_factory=list)
    trace: TraceMetadata = Field(default_factory=TraceMetadata)
