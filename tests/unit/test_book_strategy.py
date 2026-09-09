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


def test_scalp_volume_flat_is_not_a_hard_gate() -> None:
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
    assert ok is True
    assert why == "ok"


def test_day_below_session_structure_is_not_a_hard_gate() -> None:
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
    assert ok is True
    assert why == "ok"


def test_downtrend_without_oversold_is_falling_knife() -> None:
    ok, why = structure_allows_entry(
        horizon="scalp",
        trend=TrendState.DOWN,
        momentum=MomentumState.STEADY,
        liquidity=LiquidityState.NORMAL,
        volatility=VolatilityState.NORMAL,
        rsi=50.0,
    )
    assert ok is False
    assert why == "falling_knife"


def test_downtrend_oversold_bounce_allows_entry() -> None:
    ok, why = structure_allows_entry(
        horizon="scalp",
        trend=TrendState.DOWN,
        momentum=MomentumState.DECELERATING,
        liquidity=LiquidityState.NORMAL,
        volatility=VolatilityState.NORMAL,
        rsi=32.0,
        last=447.0,
        open_=449.0,
        high=451.0,
        low=446.0,
        sma_20=450.0,
    )
    assert ok is True
    assert why == "ok"


def test_uptrend_dip_scores_higher_than_chase() -> None:
    from app.universe.book_strategy import apply_timing_probability

    dip, _, dip_label = apply_timing_probability(
        0.55,
        [],
        trend=TrendState.UP,
        momentum=MomentumState.DECELERATING,
        rsi=45.0,
        last=448.5,
        high=452.0,
        low=447.0,
        sma_20=448.0,
    )
    chase, _, chase_label = apply_timing_probability(
        0.55,
        [],
        trend=TrendState.UP,
        momentum=MomentumState.ACCELERATING,
        rsi=74.0,
        last=451.5,
        high=452.0,
        low=447.0,
        sma_20=448.0,
    )
    assert dip_label == "dip_buy"
    assert chase_label == "chase"
    assert dip > chase


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


def test_quant_emits_zone_on_oversold_bounce() -> None:
    out = QuantStrategistAgent().fallback_output(
        QuantStrategistInput(
            as_of=NOW,
            symbol_bars=[
                _scalp_bar(
                    last=447.0,
                    open=449.0,
                    high=451.0,
                    low=446.0,
                    sma_20=450.0,
                    rsi_14=32.0,
                )
            ],
            watchlist=[{"symbol": "QQQ", "horizon": "scalp"}],
        ),
        reason="local_python_owns",
    )
    view = out.symbol_views[0]
    assert view.entry_zone is not None
    assert any("timing=bounce" in n for n in view.notes)


def test_quant_omits_falling_knife() -> None:
    out = QuantStrategistAgent().fallback_output(
        QuantStrategistInput(
            as_of=NOW,
            symbol_bars=[
                _scalp_bar(
                    last=447.0,
                    open=450.0,
                    high=452.0,
                    low=430.0,
                    sma_20=450.0,
                    rsi_14=55.0,
                )
            ],
            watchlist=[{"symbol": "QQQ", "horizon": "scalp"}],
        ),
        reason="local_python_owns",
    )
    view = out.symbol_views[0]
    assert view.entry_zone is None
    assert any("falling_knife" in n for n in view.notes)


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
    assert out.cash_target_pct < payload.portfolio_cash_pct
    assert out.cash_target_pct >= 30.0


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


def test_cio_fallback_enters_while_holding_when_devil_prefers_no() -> None:
    quant = QuantStrategistAgent().fallback_output(
        QuantStrategistInput(
            as_of=NOW,
            symbol_bars=[_scalp_bar()],
            watchlist=[
                {"symbol": "QQQ", "horizon": "scalp"},
                {"symbol": "SPY", "horizon": "scalp"},
            ],
        ),
        reason="local_python_owns",
    )
    payload = _cio_payload(
        quant,
        positions=[
            PositionSnapshot(
                symbol="SPY",
                quantity=10,
                market_value=4500,
                cost_basis=4400,
                unrealized_pnl=100,
                sector="index",
                weight_pct=5.0,
                venue="US",
            )
        ],
        allowlist=["QQQ", "SPY"],
        watchlist=[
            {"symbol": "QQQ", "horizon": "scalp"},
            {"symbol": "SPY", "horizon": "scalp"},
        ],
    )
    payload = payload.model_copy(
        update={
            "devil": payload.devil.model_copy(
                update={
                    "prefer_no_trade": True,
                    "prefer_no_trade_rationale": "soft wait",
                }
            )
        }
    )
    out = CIOAgent().fallback_output(payload, reason="test")
    assert any(a.symbol == "QQQ" and a.action == SymbolAction.SCALE_IN for a in out.symbol_actions)


def test_ensure_cio_takes_setups_overrides_idle_cash() -> None:
    from app.agents.cio import ensure_cio_takes_setups
    from app.schemas.cio import CIODecision

    quant = QuantStrategistAgent().fallback_output(
        QuantStrategistInput(
            as_of=NOW,
            symbol_bars=[_scalp_bar()],
            watchlist=[{"symbol": "QQQ", "horizon": "scalp"}],
        ),
        reason="local_python_owns",
    )
    idle = CIODecision(
        timestamp=NOW,
        market_regime=MarketRegime.RISK_ON,
        portfolio_action=PortfolioAction.NO_TRADE,
        cash_target_pct=100,
        risk_approval=True,
        reason_not_to_trade="wait for confirmation",
    )
    out = ensure_cio_takes_setups(
        idle,
        quant=quant,
        watchlist=[{"symbol": "QQQ", "horizon": "scalp"}],
        positions=[],
        allowlist=["QQQ"],
        risk_ok=True,
        regime=MarketRegime.RISK_ON,
        max_position_pct=10.0,
        enabled=True,
    )
    assert out.portfolio_action == PortfolioAction.SCALE_IN
    assert any(a.symbol == "QQQ" and a.action == SymbolAction.SCALE_IN for a in out.symbol_actions)
    assert out.reason_not_to_trade is None
    blocked = ensure_cio_takes_setups(
        idle,
        quant=quant,
        watchlist=[{"symbol": "QQQ", "horizon": "scalp"}],
        positions=[],
        allowlist=["QQQ"],
        risk_ok=False,
        regime=MarketRegime.RISK_ON,
        max_position_pct=10.0,
        enabled=True,
    )
    assert blocked.portfolio_action == PortfolioAction.NO_TRADE


def test_ensure_cio_fills_remaining_slots_when_cash_is_heavy() -> None:
    from app.agents.cio import ensure_cio_takes_setups
    from app.schemas.cio import CIODecision, SymbolActionPlan
    from app.schemas.common import OrderType, PriceZone

    watch = [
        {"symbol": "QQQ", "horizon": "scalp"},
        {"symbol": "SPY", "horizon": "scalp"},
    ]
    quant = QuantStrategistAgent().fallback_output(
        QuantStrategistInput(
            as_of=NOW,
            symbol_bars=[_scalp_bar(), _scalp_bar(symbol="SPY", last=500.0, sma_20=498.0)],
            watchlist=watch,
        ),
        reason="local_python_owns",
    )
    token = CIODecision(
        timestamp=NOW,
        market_regime=MarketRegime.RISK_ON,
        portfolio_action=PortfolioAction.SCALE_IN,
        cash_target_pct=90,
        risk_approval=True,
        symbol_actions=[
            SymbolActionPlan(
                symbol="QQQ",
                action=SymbolAction.SCALE_IN,
                confidence=60,
                target_position_pct=8.0,
                order_type=OrderType.LIMIT,
                entry_zone=PriceZone(min=449.0, max=451.0),
                stop_loss=445.0,
                thesis="token buy",
                invalidation="stop",
            )
        ],
    )
    out = ensure_cio_takes_setups(
        token,
        quant=quant,
        watchlist=watch,
        positions=[],
        allowlist=["QQQ", "SPY"],
        risk_ok=True,
        regime=MarketRegime.RISK_ON,
        max_position_pct=10.0,
        enabled=True,
        cash_pct=90.0,
        min_cash_pct=30.0,
    )
    symbols = {a.symbol for a in out.symbol_actions if a.action == SymbolAction.SCALE_IN}
    assert "QQQ" in symbols
    assert "SPY" in symbols
    assert out.cash_target_pct < 90.0


def test_reconcile_nameless_entry_drops_empty_scale_in() -> None:
    from app.agents.cio import reconcile_nameless_entry
    from app.schemas.cio import CIODecision

    idle = CIODecision(
        timestamp=NOW,
        market_regime=MarketRegime.RISK_ON,
        portfolio_action=PortfolioAction.SCALE_IN,
        cash_target_pct=80,
        risk_approval=True,
    )
    held = reconcile_nameless_entry(idle, has_positions=True)
    assert held.portfolio_action == PortfolioAction.HOLD
    assert "no named entry" in (held.reason_not_to_trade or "")
    flat = reconcile_nameless_entry(idle, has_positions=False)
    assert flat.portfolio_action == PortfolioAction.NO_TRADE


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


def test_day_and_scalp_stand_down_in_sideways() -> None:
    ok, why = structure_allows_entry(
        horizon="day",
        trend=TrendState.SIDEWAYS,
        momentum=MomentumState.STEADY,
        liquidity=LiquidityState.NORMAL,
        volatility=VolatilityState.NORMAL,
        rsi=55.0,
        last=100.0,
        open_=100.0,
        high=101.0,
        low=99.0,
    )
    assert ok is False
    assert why == "sideways_stand_down"
    ok, why = structure_allows_entry(
        horizon="scalp",
        trend=TrendState.SIDEWAYS,
        momentum=MomentumState.STEADY,
        liquidity=LiquidityState.NORMAL,
        volatility=VolatilityState.NORMAL,
        rsi=55.0,
        last=450.0,
        sma_20=450.0,
    )
    assert ok is False
    assert why == "sideways_stand_down"


def test_short_book_still_allows_sideways_dip() -> None:
    ok, why = structure_allows_entry(
        horizon="short",
        trend=TrendState.SIDEWAYS,
        momentum=MomentumState.STEADY,
        liquidity=LiquidityState.NORMAL,
        volatility=VolatilityState.NORMAL,
        rsi=48.0,
        last=64.0,
        sma_20=64.5,
    )
    assert ok is True
    assert why == "ok"


def test_drop_blocked_entries_strips_sideways_day_buys() -> None:
    from app.schemas.cio import CIODecision, SymbolActionPlan
    from app.schemas.common import BreadthState, OrderType
    from app.schemas.quant_strategist import QuantStrategistOutput, SymbolQuantView
    from app.universe.book_strategy import drop_blocked_entries

    decision = CIODecision(
        timestamp=NOW,
        market_regime=MarketRegime.NEUTRAL,
        portfolio_action=PortfolioAction.SCALE_IN,
        cash_target_pct=70,
        risk_approval=True,
        symbol_actions=[
            SymbolActionPlan(
                symbol="JPEQ",
                action=SymbolAction.SCALE_IN,
                confidence=60,
                target_position_pct=10,
                order_type=OrderType.LIMIT,
                stop_loss=57.0,
                thesis="fill the day book",
                invalidation="stop",
            )
        ],
    )
    quant = QuantStrategistOutput(
        timestamp=NOW,
        market_trend_state=TrendState.SIDEWAYS,
        market_momentum_state=MomentumState.STEADY,
        market_volatility_state=VolatilityState.NORMAL,
        market_breadth_state=BreadthState.MIXED,
        market_liquidity_state=LiquidityState.NORMAL,
        data_quality_score=0.8,
        symbol_views=[
            SymbolQuantView(
                symbol="JPEQ",
                trend_state=TrendState.SIDEWAYS,
                momentum_state=MomentumState.STEADY,
                volatility_state=VolatilityState.NORMAL,
                liquidity_state=LiquidityState.NORMAL,
                probability_estimate=0.62,
                probability_basis="unit",
            )
        ],
    )
    out = drop_blocked_entries(
        decision,
        quant,
        [{"symbol": "JPEQ", "horizon": "day"}],
        regime=MarketRegime.NEUTRAL,
    )
    assert out.symbol_actions == []
    assert out.portfolio_action == PortfolioAction.NO_TRADE
    assert out.reason_not_to_trade == "sideways_stand_down"
