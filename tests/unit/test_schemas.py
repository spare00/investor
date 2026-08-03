"""CIO / agent schema validation tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas import (
    CIODecision,
    DevilsAdvocateOutput,
    MacroStrategistOutput,
    MarketIntelligenceOutput,
    MarketRegime,
    PortfolioAction,
    QuantStrategistOutput,
    RiskManagerOutput,
    RiskVerdict,
    SymbolAction,
    SymbolActionPlan,
)
from app.schemas.common import (
    BreadthState,
    LiquidityState,
    MomentumState,
    OrderType,
    PriceZone,
    TrendState,
    VolatilityState,
)
from app.schemas.market_intelligence import MarketEvent
from app.schemas.common import NewsCategory, Sentiment


NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def test_market_intelligence_example_shape() -> None:
    out = MarketIntelligenceOutput(
        timestamp=NOW,
        market_events=[
            MarketEvent(
                headline="Example headline",
                source="Reuters",
                published_at=datetime(2026, 8, 3, 11, 30, tzinfo=UTC),
                symbols=["NVDA"],
                category=NewsCategory.EARNINGS,
                importance=5,
                sentiment=Sentiment.POSITIVE,
                facts=["Revenue exceeded consensus"],
                uncertainties=["Guidance quality remains unclear"],
            )
        ],
        top_market_themes=[],
        data_quality_score=0.9,
    )
    assert out.data_quality_score == 0.9
    assert out.market_events[0].symbols == ["NVDA"]


def test_macro_regime_required() -> None:
    out = MacroStrategistOutput(
        timestamp=NOW,
        market_regime=MarketRegime.RISK_ON,
        confidence=0.7,
        bullish_factors=["Soft landing"],
        bearish_factors=["Sticky services CPI"],
        expected_sector_impact=[],
        invalidation_conditions=["10Y > 5%"],
        data_quality_score=0.8,
    )
    assert out.market_regime == MarketRegime.RISK_ON


def test_quant_probability_rounded() -> None:
    from app.schemas.quant_strategist import SymbolQuantView

    view = SymbolQuantView(
        symbol="SPY",
        trend_state=TrendState.UP,
        momentum_state=MomentumState.STEADY,
        volatility_state=VolatilityState.NORMAL,
        liquidity_state=LiquidityState.NORMAL,
        probability_estimate=0.733333,
        probability_basis="rule: trend+breadth scorecard",
    )
    assert view.probability_estimate == 0.73


def test_cio_schema_happy_path() -> None:
    decision = CIODecision(
        decision_id=uuid4(),
        timestamp=NOW,
        market_regime=MarketRegime.RISK_ON,
        portfolio_action=PortfolioAction.SCALE_IN,
        symbol_actions=[
            SymbolActionPlan(
                symbol="QQQ",
                action=SymbolAction.BUY,
                confidence=72,
                target_position_pct=12,
                order_type=OrderType.LIMIT,
                entry_zone=PriceZone(min=480, max=482),
                stop_loss=474,
                take_profit=[488, 494],
                thesis="Summary",
                invalidation="Condition",
                max_holding_time_minutes=180,
            )
        ],
        cash_target_pct=35,
        hedge_required=False,
        risk_approval=True,
        risk_conditions=[],
        reason_not_to_trade=None,
    )
    assert decision.portfolio_action == PortfolioAction.SCALE_IN


def test_cio_cannot_buy_without_risk_approval() -> None:
    with pytest.raises(ValidationError):
        CIODecision(
            timestamp=NOW,
            market_regime=MarketRegime.RISK_ON,
            portfolio_action=PortfolioAction.BUY,
            symbol_actions=[],
            cash_target_pct=50,
            risk_approval=False,
        )


def test_entry_requires_stop_or_invalidation() -> None:
    with pytest.raises(ValidationError):
        SymbolActionPlan(
            symbol="QQQ",
            action=SymbolAction.BUY,
            confidence=50,
            target_position_pct=5,
            thesis="x",
            invalidation="   ",
            stop_loss=None,
        )


def test_devils_advocate_mandatory_fields() -> None:
    out = DevilsAdvocateOutput(
        timestamp=NOW,
        strongest_reason_thesis_is_wrong="Earnings already priced in",
        information_already_in_price=True,
        information_already_in_price_rationale="Gap-up on news",
        opposing_market_scenario="Fade the open into VWAP",
        prefer_no_trade=True,
        prefer_no_trade_rationale="Asymmetry poor after gap",
        immediate_withdrawal_conditions=["Break of premarket low"],
        challenge_score=0.8,
    )
    assert out.prefer_no_trade is True


def test_risk_manager_output_verdict() -> None:
    out = RiskManagerOutput(
        timestamp=NOW,
        overall_verdict=RiskVerdict.REJECTED,
        hard_vetoes=["daily_loss_limit"],
        cash_pct=40,
        gross_exposure_pct=55,
        halt_new_trades=True,
    )
    assert out.halt_new_trades is True


def test_quant_market_states() -> None:
    out = QuantStrategistOutput(
        timestamp=NOW,
        market_trend_state=TrendState.UP,
        market_momentum_state=MomentumState.STEADY,
        market_volatility_state=VolatilityState.ELEVATED,
        market_breadth_state=BreadthState.MIXED,
        market_liquidity_state=LiquidityState.NORMAL,
        data_quality_score=0.85,
    )
    assert out.market_breadth_state == BreadthState.MIXED
