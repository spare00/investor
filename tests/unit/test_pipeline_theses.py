"""Tests for quant → Devil thesis bridging."""

from __future__ import annotations

from datetime import UTC, datetime

from app.agents.pipeline import theses_from_quant
from app.schemas.common import (
    BreadthState,
    LiquidityState,
    MomentumState,
    PriceZone,
    TrendState,
    VolatilityState,
)
from app.schemas.quant_strategist import QuantStrategistOutput, SymbolQuantView


NOW = datetime(2026, 8, 6, 15, 0, tzinfo=UTC)


def test_theses_from_quant_picks_allowlisted_entry_views() -> None:
    quant = QuantStrategistOutput(
        timestamp=NOW,
        market_trend_state=TrendState.UP,
        market_momentum_state=MomentumState.STEADY,
        market_volatility_state=VolatilityState.NORMAL,
        market_breadth_state=BreadthState.HEALTHY,
        market_liquidity_state=LiquidityState.NORMAL,
        data_quality_score=0.8,
        symbol_views=[
            SymbolQuantView(
                symbol="AAPL",
                trend_state=TrendState.UP,
                momentum_state=MomentumState.STEADY,
                volatility_state=VolatilityState.NORMAL,
                liquidity_state=LiquidityState.NORMAL,
                entry_zone=PriceZone(min=100, max=101),
                stop_or_invalidation=98,
                probability_estimate=0.55,
                probability_basis="test",
            ),
            SymbolQuantView(
                symbol="ZZZ",
                trend_state=TrendState.UP,
                momentum_state=MomentumState.STEADY,
                volatility_state=VolatilityState.NORMAL,
                liquidity_state=LiquidityState.NORMAL,
                entry_zone=PriceZone(min=10, max=11),
                probability_estimate=0.9,
                probability_basis="test",
            ),
            SymbolQuantView(
                symbol="MSFT",
                trend_state=TrendState.UP,
                momentum_state=MomentumState.STEADY,
                volatility_state=VolatilityState.NORMAL,
                liquidity_state=LiquidityState.NORMAL,
                probability_estimate=0.7,
                probability_basis="test",
            ),
        ],
    )
    theses = theses_from_quant(
        quant, entry_universe=["AAPL", "MSFT"], regime="RISK_ON"
    )
    assert len(theses) == 1
    assert theses[0].symbol == "AAPL"
    assert theses[0].direction == "long"
    assert "RISK_ON" in theses[0].supporting_points
