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
    align_cio_playbook_exits,
    filter_strategy_horizons,
    horizon_for_symbol,
    notional_pct_for_risk,
    playbook_for,
    portfolio_action_from_symbol_actions,
    risk_mult_for_horizon,
    should_propose_entry,
    structure_allows_entry,
)


NOW = datetime(2026, 8, 18, 16, 0, tzinfo=UTC)


def _scalp_bar(**kwargs) -> BarSnapshot:
    row = dict(
        symbol="QQQ",
        last=450.0,
        open=448.0,
        high=451.0,
        low=447.0,
        rsi_14=58.0,
        sma_20=448.0,
        sma_50=440.0,
        sma_200=400.0,
        atr_14=4.0,
        volume=25_000_000,
        avg_volume_20d=20_000_000,
    )
    row.update(kwargs)
    return BarSnapshot(**row)


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


def test_scalp_allows_rsi_69_when_tape_accelerates() -> None:
    ok, why = structure_allows_entry(
        horizon="scalp",
        trend=TrendState.UP,
        momentum=MomentumState.ACCELERATING,
        liquidity=LiquidityState.NORMAL,
        volatility=VolatilityState.NORMAL,
        rsi=69.0,
        volume=25_000_000,
        avg_volume=20_000_000,
        last=450.0,
        sma_20=448.0,
    )
    assert ok is True
    assert why == "ok"


def test_scalp_rejects_flat_volume() -> None:
    ok, why = structure_allows_entry(
        horizon="scalp",
        trend=TrendState.UP,
        momentum=MomentumState.ACCELERATING,
        liquidity=LiquidityState.NORMAL,
        volatility=VolatilityState.NORMAL,
        rsi=58.0,
        volume=20_000_000,
        avg_volume=20_000_000,
        last=450.0,
        sma_20=448.0,
    )
    assert ok is False
    assert why == "volume_flat"


def test_day_allows_steady_session_uptrend_without_volume_accel() -> None:
    ok, why = structure_allows_entry(
        horizon="day",
        trend=TrendState.UP,
        momentum=MomentumState.STEADY,
        liquidity=LiquidityState.NORMAL,
        volatility=VolatilityState.NORMAL,
        rsi=55.0,
        volume=10_000_000,
        avg_volume=20_000_000,
        last=100.0,
        open_=99.0,
        high=101.0,
        low=98.5,
    )
    assert ok is True
    assert why == "ok"


def test_day_rejects_last_below_session_structure() -> None:
    ok, why = structure_allows_entry(
        horizon="day",
        trend=TrendState.UP,
        momentum=MomentumState.STEADY,
        liquidity=LiquidityState.NORMAL,
        volatility=VolatilityState.NORMAL,
        rsi=55.0,
        last=99.0,
        open_=100.0,
        high=102.0,
        low=98.0,
    )
    assert ok is False
    assert why in {"below_open", "below_session_vwap"}


def test_short_allows_steady_uptrend() -> None:
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


def test_risk_budget_equalizes_horizons_and_inverts_stop() -> None:
    assert risk_mult_for_horizon("scalp", firm_risk_pct=0.5) == 0.3
    assert risk_mult_for_horizon("day", firm_risk_pct=0.5) == 0.3
    assert risk_mult_for_horizon("short", firm_risk_pct=0.5) == 0.3
    tight = notional_pct_for_risk(
        horizon="scalp", entry=100.0, stop=99.0, max_position_pct=15.0
    )
    wide = notional_pct_for_risk(
        horizon="short", entry=100.0, stop=97.0, max_position_pct=15.0
    )
    # 0.15% / 1% = 15% raw, capped at scalp 8%. Wider 3% stop → 5% notional.
    assert tight == 8.0
    assert wide == 5.0
    assert tight > wide


def test_quant_omits_entry_zone_when_scalp_is_exhausted() -> None:
    out = QuantStrategistAgent().fallback_output(
        QuantStrategistInput(
            as_of=NOW,
            symbol_bars=[_scalp_bar(rsi_14=82.0)],
            watchlist=[{"symbol": "QQQ", "horizon": "scalp"}],
        ),
        reason="local_python_owns",
    )
    view = out.symbol_views[0]
    assert view.entry_zone is None
    assert any("exhausted" in n for n in view.notes)


def test_quant_keeps_scalp_entry_when_rsi_is_hot_but_not_extreme() -> None:
    out = QuantStrategistAgent().fallback_output(
        QuantStrategistInput(
            as_of=NOW,
            symbol_bars=[_scalp_bar(rsi_14=69.0)],
            watchlist=[{"symbol": "QQQ", "horizon": "scalp"}],
        ),
        reason="local_python_owns",
    )
    view = out.symbol_views[0]
    assert view.entry_zone is not None
    assert any("rsi_hot" in n for n in view.notes)


def test_cio_fallback_scales_into_scalp_not_only_hold() -> None:
    quant = QuantStrategistAgent().fallback_output(
        QuantStrategistInput(
            as_of=NOW,
            symbol_bars=[_scalp_bar()],
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
            symbol_bars=[_scalp_bar()],
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


def test_portfolio_action_promotes_hold_when_partial_sell() -> None:
    assert (
        portfolio_action_from_symbol_actions(
            [{"action": "HOLD"}, {"action": "PARTIAL_SELL"}]
        )
        == PortfolioAction.REDUCE
    )


def test_ndq_defaults_to_au_scalp() -> None:
    assert horizon_for_symbol("NDQ") == "scalp"


def test_align_blocks_short_reduce_when_swing_holds() -> None:
    from app.schemas.cio import CIODecision, SymbolActionPlan
    from app.schemas.common import BreadthState, OrderType
    from app.schemas.quant_strategist import QuantStrategistOutput, SymbolQuantView

    decision = CIODecision(
        timestamp=NOW,
        market_regime=MarketRegime.RISK_ON,
        portfolio_action=PortfolioAction.REDUCE,
        symbol_actions=[
            SymbolActionPlan(
                symbol="BHP",
                action=SymbolAction.REDUCE,
                confidence=70,
                target_position_pct=5,
                order_type=OrderType.MARKET,
                thesis="llm noise reduce",
                invalidation="n/a",
            )
        ],
        cash_target_pct=80,
        risk_approval=True,
    )
    quant = QuantStrategistOutput(
        timestamp=NOW,
        market_trend_state=TrendState.UP,
        market_momentum_state=MomentumState.STEADY,
        market_volatility_state=VolatilityState.NORMAL,
        market_breadth_state=BreadthState.MIXED,
        market_liquidity_state=LiquidityState.NORMAL,
        symbol_views=[
            SymbolQuantView(
                symbol="BHP",
                trend_state=TrendState.UP,
                momentum_state=MomentumState.STEADY,
                volatility_state=VolatilityState.NORMAL,
                liquidity_state=LiquidityState.NORMAL,
                probability_estimate=0.6,
                probability_basis="test",
            )
        ],
        data_quality_score=0.8,
    )
    out = align_cio_playbook_exits(
        decision,
        quant,
        [{"symbol": "BHP", "horizon": "short"}],
        held_symbols=["BHP"],
    )
    assert out.symbol_actions[0].action == SymbolAction.HOLD
    assert out.portfolio_action == PortfolioAction.HOLD


def test_align_flattens_day_partial_without_tape() -> None:
    from app.schemas.cio import CIODecision, SymbolActionPlan
    from app.schemas.common import BreadthState, OrderType
    from app.schemas.quant_strategist import QuantStrategistOutput

    decision = CIODecision(
        timestamp=NOW,
        market_regime=MarketRegime.RISK_ON,
        portfolio_action=PortfolioAction.REDUCE,
        symbol_actions=[
            SymbolActionPlan(
                symbol="VAS",
                action=SymbolAction.PARTIAL_SELL,
                confidence=70,
                target_position_pct=5,
                order_type=OrderType.MARKET,
                thesis="llm leftover reduce",
                invalidation="n/a",
            )
        ],
        cash_target_pct=80,
        risk_approval=True,
    )
    quant = QuantStrategistOutput(
        timestamp=NOW,
        market_trend_state=TrendState.UP,
        market_momentum_state=MomentumState.STEADY,
        market_volatility_state=VolatilityState.NORMAL,
        market_breadth_state=BreadthState.MIXED,
        market_liquidity_state=LiquidityState.NORMAL,
        symbol_views=[],
        data_quality_score=0.8,
    )
    out = align_cio_playbook_exits(
        decision,
        quant,
        [{"symbol": "VAS", "horizon": "day"}],
        held_symbols=["VAS"],
    )
    assert out.symbol_actions[0].action == SymbolAction.SELL
