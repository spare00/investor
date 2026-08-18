"""Decision-first agent briefs stay small and omit nested dumps."""

from __future__ import annotations

from datetime import UTC, datetime

from app.agents.briefs import (
    cio_brief,
    devil_brief,
    macro_brief,
    market_intelligence_brief,
    quant_brief,
    risk_brief,
)
from app.agents.cio import CIOAgent
from app.agents.market_intelligence import MarketIntelligenceAgent
from app.schemas.cio import CIOInput
from app.schemas.common import (
    BreadthState,
    LiquidityState,
    MarketRegime,
    MomentumState,
    RiskVerdict,
    Sentiment,
    TrendState,
    VolatilityState,
)
from app.schemas.devils_advocate import DevilsAdvocateInput, DevilsAdvocateOutput, ProposedThesis
from app.schemas.macro_strategist import MacroSnapshotInput, MacroStrategistInput, MacroStrategistOutput
from app.schemas.market_intelligence import (
    MarketEvent,
    MarketIntelligenceInput,
    MarketIntelligenceOutput,
    NewsItemInput,
)
from app.schemas.quant_strategist import BarSnapshot, QuantStrategistInput, QuantStrategistOutput
from app.schemas.risk_manager import (
    PortfolioStateInput,
    PositionSnapshot,
    RiskManagerInput,
    RiskManagerOutput,
)


def _now() -> datetime:
    return datetime.now(UTC)


def test_market_intel_brief_is_question_plus_compact_news() -> None:
    payload = MarketIntelligenceInput(
        as_of=_now(),
        news_items=[
            NewsItemInput(
                headline="Fed holds rates",
                source="wsj",
                published_at=_now(),
                symbols=["SPY"],
                raw_text="long unused body " * 80,
                url="https://example.invalid/x",
            )
        ],
        allowlist=["SPY", "QQQ"],
        portfolio_symbols=["BHP"],
    )
    text = market_intelligence_brief(payload)
    assert text.startswith("QUESTION:")
    assert "DATA:" in text
    assert "ANSWER:" in text
    assert "raw_text" not in text
    assert "Fed holds" in text
    assert "trace" not in text
    assert len(text) < 2500


def test_quant_brief_uses_bar_table_not_full_dump() -> None:
    payload = QuantStrategistInput(
        as_of=_now(),
        vix=16.2,
        index_bars=[
            BarSnapshot(symbol="SPY", last=500.0, rsi_14=55.0, sma_50=490.0, sma_200=480.0)
        ],
        symbol_bars=[BarSnapshot(symbol="NVDA", last=120.0, rsi_14=62.0, atr_14=3.1)],
        watchlist=[{"symbol": "NVDA", "horizon": "day", "priority": 70, "thesis": "seed " * 40}],
    )
    text = quant_brief(payload)
    assert "QUESTION:" in text
    assert "NVDA" in text
    assert "seed " not in text
    assert "trace" not in text


def test_cio_brief_summarizes_upstream_instead_of_nested_reports() -> None:
    mi = MarketIntelligenceOutput(
        timestamp=_now(),
        market_events=[
            MarketEvent(
                headline="CPI in line",
                source="bbg",
                published_at=_now(),
                importance=3,
                sentiment=Sentiment.NEUTRAL,
            )
        ],
        top_market_themes=["rates"],
        data_quality_score=0.7,
        conflicts=[],
        missing_information=[],
    )
    macro = MacroStrategistOutput(
        timestamp=_now(),
        market_regime=MarketRegime.NEUTRAL,
        confidence=0.5,
        bullish_factors=["curve"],
        bearish_factors=["cpi"],
        expected_sector_impact=[],
        invalidation_conditions=["hawkish surprise"],
        data_quality_score=0.6,
        conflicts=[],
    )
    quant = QuantStrategistOutput(
        timestamp=_now(),
        market_trend_state=TrendState.SIDEWAYS,
        market_momentum_state=MomentumState.STEADY,
        market_volatility_state=VolatilityState.NORMAL,
        market_breadth_state=BreadthState.MIXED,
        market_liquidity_state=LiquidityState.NORMAL,
        symbol_views=[],
        data_quality_score=0.8,
        conflicts=[],
    )
    risk = RiskManagerOutput(
        timestamp=_now(),
        overall_verdict=RiskVerdict.APPROVED,
        hard_vetoes=[],
        soft_warnings=[],
        trade_adjustments=[],
        halt_new_trades=False,
        cash_pct=70.0,
        gross_exposure_pct=30.0,
    )
    devil = DevilsAdvocateOutput(
        timestamp=_now(),
        strongest_reason_thesis_is_wrong="priced in",
        information_already_in_price=True,
        information_already_in_price_rationale="headline gap",
        opposing_market_scenario="fade",
        prefer_no_trade=False,
        prefer_no_trade_rationale="risk ok",
        challenge_score=0.4,
    )
    payload = CIOInput(
        as_of=_now(),
        market_intelligence=mi,
        macro=macro,
        quant=quant,
        risk=risk,
        devil=devil,
        portfolio_cash_pct=70.0,
        positions=[
            PositionSnapshot(
                symbol="BHP",
                quantity=10,
                market_value=1000,
                cost_basis=900,
                unrealized_pnl=100,
                sector="Unknown",
                weight_pct=10.0,
                venue="AU",
            )
        ],
        allowlist=["SPY"],
    )
    text = cio_brief(payload)
    assert "QUESTION:" in text
    assert "BHP" in text
    assert "market_events" not in text
    assert "prompt_sha256" not in text
    assert CIOAgent().build_user_prompt(payload) == text


def test_macro_devil_risk_briefs_start_with_question() -> None:
    macro_in = MacroStrategistInput(
        as_of=_now(),
        macro=MacroSnapshotInput(as_of=_now(), cpi_yoy=2.5, us_10y_yield=4.1, us_2y_yield=3.8),
    )
    assert macro_brief(macro_in).startswith("QUESTION:")
    devil_in = DevilsAdvocateInput(
        as_of=_now(),
        proposed_theses=[ProposedThesis(symbol="SPY", direction="long", summary="trend")],
    )
    assert "prefer_no_trade" in devil_brief(devil_in)
    risk_in = RiskManagerInput(
        as_of=_now(),
        portfolio=PortfolioStateInput(
            as_of=_now(),
            equity=1000,
            cash=700,
            cash_pct=70,
            gross_exposure_pct=30,
        ),
    )
    text = risk_brief(risk_in, {"trades": []})
    assert text.startswith("QUESTION:")
    assert "engine" in text


def test_mi_agent_uses_brief() -> None:
    payload = MarketIntelligenceInput(as_of=_now(), news_items=[], allowlist=["SPY"])
    text = MarketIntelligenceAgent().build_user_prompt(payload)
    assert text.startswith("QUESTION:")
