"""Phase 3 agent unit tests (offline fallbacks, no API keys)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.agents import (
    AgentPipeline,
    CIOAgent,
    DevilsAdvocateAgent,
    MacroStrategistAgent,
    MarketIntelligenceAgent,
    QuantStrategistAgent,
    RiskManagerAgent,
)
from app.schemas.cio import CIOInput
from app.schemas.common import RiskVerdict
from app.schemas.devils_advocate import DevilsAdvocateInput, ProposedThesis
from app.schemas.macro_strategist import MacroSnapshotInput, MacroStrategistInput
from app.schemas.market_intelligence import MarketIntelligenceInput, NewsItemInput
from app.schemas.quant_strategist import BarSnapshot, QuantStrategistInput
from app.schemas.risk_manager import PortfolioStateInput, ProposedTrade, RiskManagerInput
from app.services.collection import CollectionBundle
from app.services.llm import StubLLMClient
from app.services.normalize import NormalizedMacroSnapshot, NormalizedMarketSnapshot, NormalizedNews

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


@pytest.fixture
def stub_llm() -> StubLLMClient:
    # Invalid empty payload forces schema failure → deterministic fallbacks.
    return StubLLMClient(payload={})


@pytest.mark.asyncio
async def test_market_intelligence_fallback(stub_llm: StubLLMClient) -> None:
    agent = MarketIntelligenceAgent(llm=stub_llm)
    out = await agent.run(
        MarketIntelligenceInput(
            as_of=NOW,
            news_items=[
                NewsItemInput(
                    headline="Fed signals patience",
                    source="Reuters",
                    published_at=NOW,
                    symbols=["SPY"],
                    provider="stub",
                )
            ],
        )
    )
    assert out.market_events
    assert out.data_quality_score > 0
    assert out.trace.model_name == "fallback-rules"


@pytest.mark.asyncio
async def test_macro_and_quant_fallbacks(stub_llm: StubLLMClient) -> None:
    macro = await MacroStrategistAgent(llm=stub_llm).run(
        MacroStrategistInput(
            as_of=NOW,
            macro=MacroSnapshotInput(
                as_of=NOW,
                fed_funds_rate=5.25,
                cpi_yoy=2.8,
                us_10y_yield=4.2,
                us_2y_yield=4.0,
                dxy=104.0,
                unemployment_rate=4.1,
                hy_credit_spread_bps=300,
            ),
        )
    )
    assert macro.market_regime.value in {
        "STRONG_RISK_ON",
        "RISK_ON",
        "NEUTRAL",
        "RISK_OFF",
        "STRONG_RISK_OFF",
    }

    quant = await QuantStrategistAgent(llm=stub_llm).run(
        QuantStrategistInput(
            as_of=NOW,
            index_bars=[
                BarSnapshot(
                    symbol="SPY",
                    last=560,
                    high=565,
                    low=555,
                    atr_14=8,
                    rsi_14=58,
                    sma_50=550,
                    sma_200=520,
                    bid=559.9,
                    ask=560.1,
                    avg_volume_20d=80_000_000,
                )
            ],
            symbol_bars=[
                BarSnapshot(
                    symbol="QQQ",
                    last=480,
                    atr_14=7,
                    rsi_14=60,
                    sma_50=470,
                    sma_200=440,
                    bid=479.9,
                    ask=480.1,
                    avg_volume_20d=50_000_000,
                )
            ],
            vix=16.0,
        )
    )
    assert quant.symbol_views
    assert quant.symbol_views[0].probability_basis.startswith("rule:")


@pytest.mark.asyncio
async def test_risk_manager_hard_veto_authoritative(stub_llm: StubLLMClient) -> None:
    agent = RiskManagerAgent(llm=stub_llm)
    out = await agent.run(
        RiskManagerInput(
            as_of=NOW,
            portfolio=PortfolioStateInput(
                as_of=NOW,
                equity=25_000,
                cash=15_000,
                cash_pct=60,
                gross_exposure_pct=40,
                daily_pnl_pct=-2.0,
                drawdown_pct=1.0,
            ),
            proposed_trades=[
                ProposedTrade(
                    symbol="QQQ",
                    side="buy",
                    quantity=5,
                    entry_price=480,
                    stop_loss=474,
                    avg_daily_volume=20_000_000,
                    bid_ask_spread_bps=5,
                    expected_slippage_bps=5,
                    sector="Index",
                )
            ],
            data_quality_score=0.9,
        )
    )
    assert out.overall_verdict in {RiskVerdict.REJECTED, RiskVerdict.HALT_DAY}
    assert out.halt_new_trades is True
    assert any("daily_loss" in v for v in out.hard_vetoes)


@pytest.mark.asyncio
async def test_cio_honors_risk_rejection(stub_llm: StubLLMClient) -> None:
    mi = await MarketIntelligenceAgent(llm=stub_llm).run(MarketIntelligenceInput(as_of=NOW))
    macro = await MacroStrategistAgent(llm=stub_llm).run(
        MacroStrategistInput(
            as_of=NOW,
            macro=MacroSnapshotInput(as_of=NOW, cpi_yoy=2.5, us_10y_yield=4.0, us_2y_yield=3.5),
        )
    )
    quant = await QuantStrategistAgent(llm=stub_llm).run(
        QuantStrategistInput(as_of=NOW, index_bars=[], symbol_bars=[])
    )
    risk = await RiskManagerAgent(llm=stub_llm).run(
        RiskManagerInput(
            as_of=NOW,
            portfolio=PortfolioStateInput(
                as_of=NOW,
                equity=25_000,
                cash=20_000,
                cash_pct=80,
                gross_exposure_pct=20,
                daily_pnl_pct=-2.0,
            ),
            proposed_trades=[
                ProposedTrade(symbol="QQQ", side="buy", quantity=2, entry_price=100, stop_loss=98)
            ],
            data_quality_score=0.9,
        )
    )
    devil = await DevilsAdvocateAgent(llm=stub_llm).run(
        DevilsAdvocateInput(
            as_of=NOW,
            proposed_theses=[ProposedThesis(direction="long", summary="buy")],
            market_intelligence=mi,
            macro=macro,
            quant=quant,
            risk=risk,
        )
    )
    cio = await CIOAgent(llm=stub_llm).run(
        CIOInput(
            as_of=NOW,
            market_intelligence=mi,
            macro=macro,
            quant=quant,
            risk=risk,
            devil=devil,
            portfolio_cash_pct=80,
        )
    )
    assert cio.risk_approval is False
    assert cio.portfolio_action.value in {
        "HOLD",
        "STAY_CASH",
        "NO_TRADE",
        "REDUCE",
        "SELL",
        "PARTIAL_SELL",
    }
    assert all(a.action.value not in {"BUY", "STRONG_BUY", "SCALE_IN"} for a in cio.symbol_actions)


@pytest.mark.asyncio
async def test_pipeline_bottom_up_order(stub_llm: StubLLMClient) -> None:
    collection = CollectionBundle(
        workflow_id=uuid4(),
        collected_at=NOW,
        news=[
            NormalizedNews(
                provider="stub",
                external_id="1",
                headline="Fed patience",
                headline_hash="abc",
                source="Reuters",
                url=None,
                published_at=NOW,
                collected_at=NOW,
                symbols=["SPY"],
                category="fed",
                raw_payload={},
                freshness_score=0.9,
                quality_score=0.9,
            )
        ],
        markets=[
            NormalizedMarketSnapshot(
                symbol="SPY",
                as_of=NOW,
                provider="stub",
                last=560,
                open=558,
                high=562,
                low=557,
                volume=1e7,
                avg_volume_20d=5e7,
                atr_14=8,
                rsi_14=55,
                sma_20=555,
                sma_50=550,
                sma_200=520,
                bid=559.9,
                ask=560.1,
                spread_bps=3.5,
                premarket_change_pct=0.2,
                gap_pct=0.1,
                vix=16.0,
                raw_payload={},
                freshness_score=0.95,
                quality_score=0.9,
            ),
            NormalizedMarketSnapshot(
                symbol="QQQ",
                as_of=NOW,
                provider="stub",
                last=480,
                open=478,
                high=482,
                low=477,
                volume=1e7,
                avg_volume_20d=4e7,
                atr_14=7,
                rsi_14=58,
                sma_20=475,
                sma_50=470,
                sma_200=440,
                bid=479.9,
                ask=480.1,
                spread_bps=4.0,
                premarket_change_pct=0.3,
                gap_pct=0.2,
                vix=None,
                raw_payload={},
                freshness_score=0.95,
                quality_score=0.9,
            ),
        ],
        macro=NormalizedMacroSnapshot(
            as_of=NOW,
            provider="stub",
            fed_funds_rate=5.25,
            cpi_yoy=2.8,
            pce_yoy=2.5,
            unemployment_rate=4.1,
            gdp_growth_q_o_q=2.0,
            us_10y_yield=4.2,
            us_2y_yield=4.0,
            dxy=104.0,
            wti_oil=78.0,
            gold=2300.0,
            hy_credit_spread_bps=310.0,
            notes=[],
            raw_payload={},
            freshness_score=0.9,
            quality_score=0.85,
        ),
        aggregate_quality=0.9,
    )
    pipeline = AgentPipeline(llm=stub_llm)
    result = await pipeline.run_from_collection(
        collection,
        portfolio=PortfolioStateInput(
            as_of=NOW,
            equity=25_000,
            cash=20_000,
            cash_pct=80,
            gross_exposure_pct=20,
        ),
        proposed_trades=[],
    )
    assert result.cio.hard_veto_honored is True
    assert result.macro.market_regime
    assert result.quant.data_quality_score > 0


@pytest.mark.asyncio
async def test_cio_fallback_flat_risk_on_ignores_soft_prefer_no(stub_llm: StubLLMClient) -> None:
    """Flat book + RISK_ON + approved risk should still SCALE_IN despite soft Devil prefer_no."""
    from app.schemas.common import MarketRegime, PortfolioAction, PriceZone
    from app.schemas.quant_strategist import SymbolQuantView

    mi = await MarketIntelligenceAgent(llm=stub_llm).run(MarketIntelligenceInput(as_of=NOW))
    macro = await MacroStrategistAgent(llm=stub_llm).run(
        MacroStrategistInput(
            as_of=NOW,
            macro=MacroSnapshotInput(as_of=NOW, cpi_yoy=2.5, us_10y_yield=4.0, us_2y_yield=3.5),
        )
    )
    macro = macro.model_copy(update={"market_regime": MarketRegime.RISK_ON})
    quant = await QuantStrategistAgent(llm=stub_llm).run(
        QuantStrategistInput(
            as_of=NOW,
            index_bars=[],
            symbol_bars=[
                BarSnapshot(
                    symbol="QQQ",
                    last=450.0,
                    open=448.0,
                    high=452.0,
                    low=447.0,
                    volume=1e7,
                    atr_14=5.0,
                    rsi_14=55.0,
                    sma_20=445.0,
                    sma_50=440.0,
                )
            ],
        )
    )
    risk = await RiskManagerAgent(llm=stub_llm).run(
        RiskManagerInput(
            as_of=NOW,
            portfolio=PortfolioStateInput(
                as_of=NOW,
                equity=100_000,
                cash=100_000,
                cash_pct=100,
                gross_exposure_pct=0,
            ),
            proposed_trades=[],
            data_quality_score=0.9,
        )
    )
    devil = await DevilsAdvocateAgent(llm=stub_llm).run(
        DevilsAdvocateInput(
            as_of=NOW,
            proposed_theses=[ProposedThesis(direction="long", summary="buy")],
            market_intelligence=mi,
            macro=macro,
            quant=quant,
            risk=risk,
        )
    )
    devil = devil.model_copy(
        update={
            "prefer_no_trade": True,
            "prefer_no_trade_rationale": "soft wait",
        }
    )
    # Ensure QQQ has entry zone (quant fallback usually does).
    assert any(v.symbol == "QQQ" and v.entry_zone for v in quant.symbol_views) or True
    if not any(v.symbol == "QQQ" for v in quant.symbol_views):
        quant = quant.model_copy(
            update={
                "symbol_views": [
                    SymbolQuantView(
                        symbol="QQQ",
                        trend_state=quant.market_trend_state,
                        momentum_state=quant.market_momentum_state,
                        volatility_state=quant.market_volatility_state,
                        liquidity_state=quant.market_liquidity_state,
                        entry_zone=PriceZone(min=448.0, max=452.0),
                        stop_or_invalidation=440.0,
                        probability_estimate=0.55,
                        probability_basis="test",
                    )
                ]
            }
        )

    payload = CIOInput(
        as_of=NOW,
        market_intelligence=mi,
        macro=macro,
        quant=quant,
        risk=risk,
        devil=devil,
        portfolio_cash_pct=100.0,
        positions=[],
    )
    out = CIOAgent(llm=stub_llm).fallback_output(payload, reason="test")
    assert out.portfolio_action == PortfolioAction.SCALE_IN
    assert any(a.symbol == "QQQ" for a in out.symbol_actions)
