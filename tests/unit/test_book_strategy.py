"""Per-book playbooks: scalp / day / short are not the same trade."""

from __future__ import annotations

from datetime import UTC, datetime

from app.agents.cio import CIOAgent
from app.agents.quant_strategist import QuantStrategistAgent
from app.schemas.cio import CIOInput
from app.schemas.common import (
    LiquidityState,
    MarketRegime,
    MomentumState,
    PortfolioAction,
    RiskVerdict,
    SymbolAction,
    TrendState,
    VolatilityState,
)
from app.schemas.devils_advocate import DevilsAdvocateOutput
from app.schemas.macro_strategist import MacroStrategistOutput
from app.schemas.market_intelligence import MarketIntelligenceOutput
from app.schemas.quant_strategist import BarSnapshot, QuantStrategistInput
from app.schemas.risk_manager import PositionSnapshot, RiskManagerOutput
from app.universe.book_strategy import (
    filter_strategy_horizons,
    horizon_for_symbol,
    playbook_for,
    should_propose_entry,
    structure_allows_entry,
)


NOW = datetime(2026, 8, 18, 16, 0, tzinfo=UTC)


def _cio_payload(quant, *, positions=None, allowlist=None, watchlist=None) -> CIOInput:
    return CIOInput(
        as_of=NOW,
        market_intelligence=MarketIntelligenceOutput(timestamp=NOW, data_quality_score=0.8),
        macro=MacroStrategistOutput(
            timestamp=NOW,
            market_regime=MarketRegime.RISK_ON,
            confidence=0.7,
            data_quality_score=0.8,
        ),
        quant=quant,
        risk=RiskManagerOutput(
            timestamp=NOW,
            overall_verdict=RiskVerdict.APPROVED,
            halt_new_trades=False,
            cash_pct=80.0,
            gross_exposure_pct=20.0,
        ),
        devil=DevilsAdvocateOutput(
            timestamp=NOW,
            strongest_reason_thesis_is_wrong="none",
            information_already_in_price=False,
            information_already_in_price_rationale="n/a",
            opposing_market_scenario="fade",
            prefer_no_trade=False,
            prefer_no_trade_rationale="",
            challenge_score=0.2,
        ),
        portfolio_cash_pct=80.0,
        positions=positions or [],
        allowlist=allowlist or ["QQQ", "SPY"],
        watchlist=watchlist or [{"symbol": "QQQ", "horizon": "scalp"}],
    )


def test_filter_drops_medium() -> None:
    assert filter_strategy_horizons(["scalp", "medium", "day", "medium"]) == ["scalp", "day"]


def test_qqq_defaults_to_scalp() -> None:
    assert horizon_for_symbol("QQQ") == "scalp"
    assert playbook_for("scalp") is not None
    assert playbook_for("medium") is None


def test_scalp_rejects_exhaustion_day_allows_steady_uptrend() -> None:
    ok_s, why_s = structure_allows_entry(
        horizon="scalp",
        trend=TrendState.UP,
        momentum=MomentumState.EXHAUSTED,
        liquidity=LiquidityState.NORMAL,
        volatility=VolatilityState.NORMAL,
        rsi=72.0,
    )
    assert ok_s is False
    ok_d, _ = structure_allows_entry(
        horizon="day",
        trend=TrendState.UP,
        momentum=MomentumState.STEADY,
        liquidity=LiquidityState.NORMAL,
        volatility=VolatilityState.NORMAL,
        rsi=55.0,
    )
    assert ok_d is True
    assert should_propose_entry(
        horizon="short",
        probability=0.6,
        trend=TrendState.UP,
        momentum=MomentumState.STEADY,
        liquidity=LiquidityState.NORMAL,
        volatility=VolatilityState.NORMAL,
        rsi=50.0,
        regime=MarketRegime.RISK_ON,
    )


def test_quant_omits_entry_zone_when_scalp_is_exhausted() -> None:
    out = QuantStrategistAgent().fallback_output(
        QuantStrategistInput(
            as_of=NOW,
            symbol_bars=[
                BarSnapshot(
                    symbol="QQQ",
                    last=450.0,
                    rsi_14=78.0,
                    sma_50=440.0,
                    sma_200=400.0,
                    atr_14=4.0,
                    avg_volume_20d=20_000_000,
                )
            ],
            watchlist=[{"symbol": "QQQ", "horizon": "scalp"}],
        ),
        reason="local_python_owns",
    )
    view = out.symbol_views[0]
    assert view.entry_zone is None
    assert any("exhausted" in n or "rsi_high" in n for n in view.notes)


def test_cio_fallback_scales_into_scalp_not_only_hold() -> None:
    quant = QuantStrategistAgent().fallback_output(
        QuantStrategistInput(
            as_of=NOW,
            symbol_bars=[
                BarSnapshot(
                    symbol="QQQ",
                    last=450.0,
                    rsi_14=58.0,
                    sma_50=440.0,
                    sma_200=400.0,
                    atr_14=4.0,
                    avg_volume_20d=20_000_000,
                )
            ],
            watchlist=[{"symbol": "QQQ", "horizon": "scalp"}],
        ),
        reason="local_python_owns",
    )
    assert quant.symbol_views[0].entry_zone is not None
    payload = _cio_payload(quant)
    out = CIOAgent().fallback_output(payload, reason="test")
    assert out.portfolio_action == PortfolioAction.SCALE_IN
    assert any(a.symbol == "QQQ" and a.action == SymbolAction.SCALE_IN for a in out.symbol_actions)


def test_cio_fallback_ignores_other_venue_positions() -> None:
    quant = QuantStrategistAgent().fallback_output(
        QuantStrategistInput(
            as_of=NOW,
            symbol_bars=[
                BarSnapshot(symbol="QQQ", last=450.0, rsi_14=58.0, sma_50=440.0, sma_200=400.0, atr_14=4.0)
            ],
            watchlist=[{"symbol": "QQQ", "horizon": "scalp"}],
        ),
        reason="local_python_owns",
    )
    payload = _cio_payload(
        quant,
        positions=[
            PositionSnapshot(
                symbol="BHP",
                quantity=100,
                market_value=6000,
                cost_basis=5800,
                unrealized_pnl=200,
                sector="materials",
                weight_pct=10.0,
                venue="AU",
            )
        ],
    )
    out = CIOAgent().fallback_output(payload, reason="test")
    assert all(a.symbol != "BHP" for a in out.symbol_actions)
    assert any(a.symbol == "QQQ" for a in out.symbol_actions)
