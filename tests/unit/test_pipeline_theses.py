"""Tests for quant → Devil thesis bridging and CIO stop enrichment."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.agents.pipeline import enrich_cio_entry_stops, theses_from_quant
from app.schemas.cio import CIODecision, SymbolActionPlan
from app.schemas.common import (
    BreadthState,
    LiquidityState,
    MarketRegime,
    MomentumState,
    OrderType,
    PortfolioAction,
    PriceZone,
    SymbolAction,
    TimeHorizon,
    TraceMetadata,
    TrendState,
    VolatilityState,
)
from app.schemas.quant_strategist import QuantStrategistOutput, SymbolQuantView


NOW = datetime(2026, 8, 6, 15, 0, tzinfo=UTC)


def _quant(*views: SymbolQuantView) -> QuantStrategistOutput:
    return QuantStrategistOutput(
        timestamp=NOW,
        market_trend_state=TrendState.UP,
        market_momentum_state=MomentumState.STEADY,
        market_volatility_state=VolatilityState.NORMAL,
        market_breadth_state=BreadthState.HEALTHY,
        market_liquidity_state=LiquidityState.NORMAL,
        data_quality_score=0.8,
        symbol_views=list(views),
    )


def test_theses_from_quant_picks_allowlisted_entry_views() -> None:
    quant = _quant(
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
    )
    theses = theses_from_quant(
        quant, entry_universe=["AAPL", "MSFT"], regime="RISK_ON"
    )
    assert len(theses) == 1
    assert theses[0].symbol == "AAPL"
    assert theses[0].direction == "long"
    assert "RISK_ON" in theses[0].supporting_points


def test_enrich_cio_entry_stops_from_quant_and_price() -> None:
    quant = _quant(
        SymbolQuantView(
            symbol="AAPL",
            trend_state=TrendState.UP,
            momentum_state=MomentumState.STEADY,
            volatility_state=VolatilityState.NORMAL,
            liquidity_state=LiquidityState.NORMAL,
            entry_zone=PriceZone(min=100, max=101),
            stop_or_invalidation=97.5,
            probability_estimate=0.75,
            probability_basis="test",
        ),
        SymbolQuantView(
            symbol="MSFT",
            trend_state=TrendState.UP,
            momentum_state=MomentumState.STEADY,
            volatility_state=VolatilityState.NORMAL,
            liquidity_state=LiquidityState.NORMAL,
            entry_zone=PriceZone(min=400, max=402),
            probability_estimate=0.7,
            probability_basis="test",
        ),
    )
    decision = CIODecision(
        decision_id=uuid4(),
        timestamp=NOW,
        market_regime=MarketRegime.RISK_ON,
        portfolio_action=PortfolioAction.SCALE_IN,
        symbol_actions=[
            SymbolActionPlan(
                symbol="AAPL",
                action=SymbolAction.BUY,
                confidence=75,
                target_position_pct=5.0,
                order_type=OrderType.LIMIT,
                entry_zone=PriceZone(min=100, max=101),
                stop_loss=None,
                thesis="buy AAPL",
                invalidation="break premarket lows",
                time_horizon=TimeHorizon.INTRADAY,
            ),
            SymbolActionPlan(
                symbol="MSFT",
                action=SymbolAction.SCALE_IN,
                confidence=70,
                target_position_pct=5.0,
                order_type=OrderType.LIMIT,
                entry_zone=PriceZone(min=400, max=402),
                stop_loss=None,
                thesis="scale MSFT",
                invalidation="n/a",
                time_horizon=TimeHorizon.SWING,
            ),
            SymbolActionPlan(
                symbol="NVDA",
                action=SymbolAction.BUY,
                confidence=70,
                target_position_pct=5.0,
                order_type=OrderType.LIMIT,
                stop_loss=None,
                thesis="buy NVDA",
                invalidation="vol spike",
                time_horizon=TimeHorizon.INTRADAY,
            ),
            SymbolActionPlan(
                symbol="META",
                action=SymbolAction.HOLD,
                confidence=50,
                target_position_pct=0.0,
                order_type=OrderType.LIMIT,
                thesis="hold",
                invalidation="n/a",
                time_horizon=TimeHorizon.INTRADAY,
            ),
        ],
        cash_target_pct=0.0,
        hedge_required=False,
        risk_approval=True,
        risk_conditions=[],
        reason_not_to_trade=None,
        hard_veto_honored=True,
        trace=TraceMetadata(source_data_timestamp=NOW),
    )
    out = enrich_cio_entry_stops(
        decision, quant, latest_prices={"NVDA": 100.0, "AAPL": 100.5}
    )
    by_sym = {p.symbol: p for p in out.symbol_actions}
    assert by_sym["AAPL"].stop_loss == 97.5
    assert by_sym["MSFT"].stop_loss == 392.0  # default 2% below entry min
    assert by_sym["NVDA"].stop_loss == 98.0  # default 2% below last
    assert by_sym["META"].stop_loss is None


def test_enrich_cio_entry_stops_horizon_aware() -> None:
    quant = _quant()
    decision = CIODecision(
        decision_id=uuid4(),
        timestamp=NOW,
        market_regime=MarketRegime.RISK_ON,
        portfolio_action=PortfolioAction.SCALE_IN,
        symbol_actions=[
            SymbolActionPlan(
                symbol="QQQ",
                action=SymbolAction.BUY,
                confidence=70,
                target_position_pct=5.0,
                order_type=OrderType.LIMIT,
                entry_zone=PriceZone(min=100, max=101),
                stop_loss=None,
                thesis="scalp",
                invalidation="n/a",
                time_horizon=TimeHorizon.INTRADAY,
            ),
            SymbolActionPlan(
                symbol="MSFT",
                action=SymbolAction.BUY,
                confidence=70,
                target_position_pct=5.0,
                order_type=OrderType.LIMIT,
                entry_zone=PriceZone(min=100, max=101),
                stop_loss=None,
                thesis="medium",
                invalidation="n/a",
                time_horizon=TimeHorizon.POSITION,
            ),
            SymbolActionPlan(
                symbol="AAPL",
                action=SymbolAction.BUY,
                confidence=70,
                target_position_pct=5.0,
                order_type=OrderType.LIMIT,
                entry_zone=PriceZone(min=100, max=101),
                stop_loss=99.5,  # too tight for medium book
                thesis="widen me",
                invalidation="structure",
                time_horizon=TimeHorizon.POSITION,
            ),
        ],
        cash_target_pct=90.0,
        risk_approval=True,
        trace=TraceMetadata(source_data_timestamp=NOW),
    )
    out = enrich_cio_entry_stops(
        decision,
        quant,
        watchlist_context=[
            {"symbol": "QQQ", "horizon": "scalp"},
            {"symbol": "MSFT", "horizon": "medium"},
            {"symbol": "AAPL", "horizon": "medium"},
        ],
        atr_by_symbol={"QQQ": 1.0, "MSFT": 2.0},
    )
    by_sym = {p.symbol: p for p in out.symbol_actions}
    assert by_sym["QQQ"].stop_loss == 99.0  # 1× ATR
    assert by_sym["MSFT"].stop_loss == 93.0  # 3.5× ATR
    assert by_sym["AAPL"].stop_loss == 97.5  # widened to medium min_stop 2.5%
