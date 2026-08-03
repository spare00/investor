"""Quant & Technical Strategist I/O schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from app.schemas.common import (
    BreadthState,
    LiquidityState,
    MomentumState,
    PriceZone,
    Scenario,
    StrictModel,
    TraceMetadata,
    TrendState,
    VolatilityState,
)


class BarSnapshot(StrictModel):
    symbol: str
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


class QuantStrategistInput(StrictModel):
    as_of: datetime
    index_bars: list[BarSnapshot] = Field(default_factory=list)
    sector_etf_bars: list[BarSnapshot] = Field(default_factory=list)
    symbol_bars: list[BarSnapshot] = Field(default_factory=list)
    vix: float | None = None
    advance_decline: float | None = None
    market_intelligence_summary: dict[str, object] | None = None
    trace: TraceMetadata = Field(default_factory=TraceMetadata)


class SymbolQuantView(StrictModel):
    symbol: str
    trend_state: TrendState
    momentum_state: MomentumState
    volatility_state: VolatilityState
    breadth_state: BreadthState | None = None
    liquidity_state: LiquidityState
    support: float | None = None
    resistance: float | None = None
    entry_zone: PriceZone | None = None
    stop_or_invalidation: float | None = None
    upside_scenario: Scenario | None = None
    downside_scenario: Scenario | None = None
    probability_estimate: float = Field(ge=0.0, le=1.0)
    probability_basis: str
    notes: list[str] = Field(default_factory=list)

    @field_validator("probability_estimate")
    @classmethod
    def _round_probability(cls, value: float) -> float:
        # Discourage false precision from free-form LLM numbers.
        return round(value, 2)


class QuantStrategistOutput(StrictModel):
    timestamp: datetime
    market_trend_state: TrendState
    market_momentum_state: MomentumState
    market_volatility_state: VolatilityState
    market_breadth_state: BreadthState
    market_liquidity_state: LiquidityState
    symbol_views: list[SymbolQuantView] = Field(default_factory=list)
    data_quality_score: float = Field(ge=0.0, le=1.0)
    conflicts: list[str] = Field(default_factory=list)
    trace: TraceMetadata = Field(default_factory=TraceMetadata)
