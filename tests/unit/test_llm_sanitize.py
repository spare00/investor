"""Tests for LLM payload sanitization."""

from __future__ import annotations

from app.agents.llm_sanitize import coerce_enum_value, sanitize_llm_payload
from app.schemas.common import NewsCategory, Sentiment, TrendState, VolatilityState
from app.schemas.market_intelligence import MarketIntelligenceOutput
from app.schemas.quant_strategist import QuantStrategistOutput


def test_coerce_sentiment_case_and_synonym() -> None:
    assert coerce_enum_value(Sentiment, "Neutral") is Sentiment.NEUTRAL
    assert coerce_enum_value(Sentiment, "bullish") is Sentiment.POSITIVE


def test_coerce_news_category_free_text() -> None:
    assert coerce_enum_value(NewsCategory, "Monetary Policy") is NewsCategory.FED
    assert coerce_enum_value(NewsCategory, "Something Weird") is NewsCategory.OTHER


def test_sanitize_market_intelligence_payload() -> None:
    data = sanitize_llm_payload(
        {
            "market_events": [
                {
                    "headline": "Fed holds",
                    "source": "Reuters",
                    "published_at": "2026-08-03T12:00:00Z",
                    "category": "Monetary Policy",
                    "sentiment": "Neutral",
                    "importance": 4,
                }
            ],
            "top_market_themes": ["rates"],
            "data_quality_score": 4,
        }
    )
    out = MarketIntelligenceOutput.model_validate(data)
    assert out.market_events[0].category is NewsCategory.FED
    assert out.market_events[0].sentiment is Sentiment.NEUTRAL
    assert out.data_quality_score == 1.0


def test_sanitize_agent_shape_coercions() -> None:
    from app.schemas.devils_advocate import DevilsAdvocateOutput
    from app.schemas.macro_strategist import MacroStrategistOutput
    from app.schemas.risk_manager import RiskManagerOutput

    mi = MarketIntelligenceOutput.model_validate(
        sanitize_llm_payload(
            {
                "market_events": [],
                "top_market_themes": ["ai"],
                "missing_information": ["quotes"],
            }
        )
    )
    assert mi.data_quality_score == 0.3

    macro = MacroStrategistOutput.model_validate(
        sanitize_llm_payload(
            {
                "market_regime": "RISK_ON",
                "confidence": 0.6,
                "sector_impacts": {"technology": "Potential upside from AI"},
                "data_quality_score": 0.8,
            }
        )
    )
    assert macro.expected_sector_impact[0].sector == "technology"

    quant = QuantStrategistOutput.model_validate(
        sanitize_llm_payload(
            {
                "market_trend_state": "up",
                "market_momentum_state": "steady",
                "market_volatility_state": "normal",
                "market_breadth_state": "healthy",
                "market_liquidity_state": "normal",
                "data_quality_score": 0.7,
                "symbol_views": [
                    {
                        "symbol": "QQQ",
                        "trend_state": "up",
                        "momentum_state": "steady",
                        "volatility_state": "normal",
                        "liquidity_state": "normal",
                        "probability_estimate": 0.55,
                        "probability_basis": "test",
                        "scenarios": [
                            {
                                "name": "upside",
                                "description": "rally",
                                "probability": 0.6,
                            },
                            {
                                "name": "downside",
                                "description": "selloff",
                                "probability": 0.4,
                            },
                        ],
                    }
                ],
            }
        )
    )
    assert quant.symbol_views[0].upside_scenario is not None
    assert quant.symbol_views[0].downside_scenario is not None

    risk = RiskManagerOutput.model_validate(
        sanitize_llm_payload(
            {
                "overall_verdict": "approved",
                "data_quality_score": 0.9,
                "hard_vetoes": [],
            }
        )
    )
    assert risk.cash_pct == 50.0

    devil = DevilsAdvocateOutput.model_validate(
        sanitize_llm_payload(
            {
                "strongest_reason_thesis_is_wrong": "priced in",
                "opposing_market_scenario": "fade",
                "challenge_score": 0.8,
                "recommendation": "WAIT",
            }
        )
    )
    assert devil.prefer_no_trade is True


def test_sanitize_quant_synonyms_and_aliases() -> None:
    data = sanitize_llm_payload(
        {
            "market_trend_state": "bullish",
            "market_momentum_state": "neutral",
            "market_volatility_state": "moderate",
            "market_breadth_state": "healthy",
            "market_liquidity_state": "adequate",
            "data_quality_score": 0.8,
            "symbol_views": [
                {
                    "symbol": "QQQ",
                    "trend": "up",
                    "momentum_state": "steady",
                    "volatility_state": "normal",
                    "liquidity_state": "normal",
                    "probability_estimate": 0.55,
                    "probability_basis": "test",
                    "entry_zone": 100,
                }
            ],
        }
    )
    out = QuantStrategistOutput.model_validate(data)
    assert out.market_trend_state is TrendState.UP
    assert out.market_volatility_state is VolatilityState.NORMAL
    assert out.symbol_views[0].trend_state is TrendState.UP
    assert out.symbol_views[0].entry_zone is not None
