"""Entry-reason expectancy and attribution stamps."""

from __future__ import annotations

from datetime import UTC, datetime

from app.agents.cio import ensure_cio_takes_setups
from app.performance.entry_expectancy import compute_entry_reason_expectancy, modeled_cost
from app.performance.trades import ClosedTrade
from app.schemas.cio import CIODecision
from app.schemas.common import (
    BreadthState,
    LiquidityState,
    MarketRegime,
    MomentumState,
    PortfolioAction,
    TrendState,
    VolatilityState,
)
from app.schemas.quant_strategist import QuantStrategistOutput, SymbolQuantView
from app.universe.entry_attribution import (
    SOURCE_INJECTED,
    classify_exit_reason,
    public_entry_reason,
    stamp_cio_entry_attribution,
    stamp_lifecycle_exit_reason,
    timing_from_view,
)


def test_timing_labels_map_to_operator_reasons() -> None:
    assert public_entry_reason("dip_buy") == "uptrend_dip"
    assert public_entry_reason("bounce") == "oversold_bounce"
    assert public_entry_reason("chase") == "chase"
    assert public_entry_reason("accumulation") == "stealth_buy"
    assert public_entry_reason("continuation") is None


def test_exit_reason_classifies_session_flatten_and_giveback() -> None:
    assert classify_exit_reason(thesis="closing:intraday-only position") == "session_flatten"
    assert classify_exit_reason(reason="force_close:intraday-only") == "session_flatten"
    assert classify_exit_reason(reason="giveback_to_loss") == "giveback_exit"
    assert classify_exit_reason(reason="take_profit_triggered") == "take_profit"
    assert classify_exit_reason(reason="hard_stop") == "stop"


def test_stamp_exit_reason_does_not_overwrite() -> None:
    class _Lc:
        metadata_json = {"exit_reason": "take_profit"}

    lc = _Lc()
    assert stamp_lifecycle_exit_reason(lc, raw="closing:intraday-only") == "take_profit"


def test_injected_plans_carry_source_and_timing() -> None:
    view = SymbolQuantView(
        symbol="QQQ",
        trend_state=TrendState.UP,
        momentum_state=MomentumState.STEADY,
        volatility_state=VolatilityState.NORMAL,
        liquidity_state=LiquidityState.NORMAL,
        support=440.0,
        resistance=452.0,
        probability_estimate=0.6,
        probability_basis="test",
        notes=["timing=dip_buy"],
        entry_timing="dip_buy",
    )
    quant = QuantStrategistOutput(
        timestamp=datetime.now(UTC),
        market_trend_state=TrendState.UP,
        market_momentum_state=MomentumState.STEADY,
        market_volatility_state=VolatilityState.NORMAL,
        market_breadth_state=BreadthState.MIXED,
        market_liquidity_state=LiquidityState.NORMAL,
        symbol_views=[view],
        data_quality_score=0.8,
    )
    # Direct stamp path (quant_entry_plans needs a zone + stop).
    from app.schemas.common import PriceZone

    view = view.model_copy(
        update={"entry_zone": PriceZone(min=449.0, max=451.0), "stop_or_invalidation": 445.0}
    )
    quant = quant.model_copy(update={"symbol_views": [view]})
    idle = CIODecision(
        timestamp=datetime.now(UTC),
        market_regime=MarketRegime.RISK_ON,
        portfolio_action=PortfolioAction.NO_TRADE,
        cash_target_pct=100,
        risk_approval=True,
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
    plan = next(p for p in out.symbol_actions if p.symbol == "QQQ")
    assert plan.entry_source == SOURCE_INJECTED
    assert plan.entry_timing == "dip_buy"
    assert plan.trend_at_entry == "up"


def test_short_bounce_and_sideways_are_not_injected() -> None:
    from app.schemas.common import PriceZone

    bounce = SymbolQuantView(
        symbol="CBA",
        trend_state=TrendState.DOWN,
        momentum_state=MomentumState.STEADY,
        volatility_state=VolatilityState.NORMAL,
        liquidity_state=LiquidityState.NORMAL,
        support=100.0,
        resistance=110.0,
        probability_estimate=0.6,
        probability_basis="test",
        notes=["timing=bounce"],
        entry_timing="bounce",
        entry_zone=PriceZone(min=101.0, max=103.0),
        stop_or_invalidation=97.0,
    )
    sideways = bounce.model_copy(
        update={"symbol": "BHP", "trend_state": TrendState.SIDEWAYS, "entry_timing": "dip_buy"}
    )
    quant = QuantStrategistOutput(
        timestamp=datetime.now(UTC),
        market_trend_state=TrendState.SIDEWAYS,
        market_momentum_state=MomentumState.STEADY,
        market_volatility_state=VolatilityState.NORMAL,
        market_breadth_state=BreadthState.MIXED,
        market_liquidity_state=LiquidityState.NORMAL,
        symbol_views=[bounce, sideways],
        data_quality_score=0.8,
    )
    idle = CIODecision(
        timestamp=datetime.now(UTC),
        market_regime=MarketRegime.RISK_ON,
        portfolio_action=PortfolioAction.NO_TRADE,
        cash_target_pct=100,
        risk_approval=True,
    )
    out = ensure_cio_takes_setups(
        idle,
        quant=quant,
        watchlist=[{"symbol": "CBA", "horizon": "short"}, {"symbol": "BHP", "horizon": "short"}],
        positions=[],
        allowlist=["CBA", "BHP"],
        risk_ok=True,
        regime=MarketRegime.RISK_ON,
        max_position_pct=10.0,
        enabled=True,
    )
    names = {p.symbol for p in out.symbol_actions if p.action.value in {"BUY", "SCALE_IN", "STRONG_BUY"}}
    assert "CBA" not in names
    assert "BHP" not in names


def test_stamp_cio_fills_cio_source_from_quant() -> None:
    from app.schemas.cio import SymbolActionPlan
    from app.schemas.common import OrderType, PriceZone, SymbolAction, TimeHorizon

    view = SymbolQuantView(
        symbol="QQQ",
        trend_state=TrendState.UP,
        momentum_state=MomentumState.STEADY,
        volatility_state=VolatilityState.NORMAL,
        liquidity_state=LiquidityState.NORMAL,
        support=440.0,
        resistance=452.0,
        probability_estimate=0.62,
        probability_basis="test",
        notes=["timing=dip_buy"],
        entry_timing="dip_buy",
        entry_zone=PriceZone(min=449.0, max=451.0),
        stop_or_invalidation=445.0,
    )
    quant = QuantStrategistOutput(
        timestamp=datetime.now(UTC),
        market_trend_state=TrendState.UP,
        market_momentum_state=MomentumState.STEADY,
        market_volatility_state=VolatilityState.NORMAL,
        market_breadth_state=BreadthState.HEALTHY,
        market_liquidity_state=LiquidityState.NORMAL,
        symbol_views=[view],
        data_quality_score=0.8,
    )
    decision = CIODecision(
        timestamp=datetime.now(UTC),
        market_regime=MarketRegime.RISK_ON,
        portfolio_action=PortfolioAction.SCALE_IN,
        cash_target_pct=70,
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
                thesis="manual",
                invalidation="stop",
                time_horizon=TimeHorizon.INTRADAY,
            )
        ],
    )
    out = stamp_cio_entry_attribution(decision, quant)
    plan = out.symbol_actions[0]
    assert plan.entry_source == "cio"
    assert plan.entry_timing == "dip_buy"
    assert timing_from_view(view) == "dip_buy"


def test_gate_expectancy_splits_reasons_and_cohorts() -> None:
    trades = [
        ClosedTrade(
            pnl=80.0,
            holding_minutes=40,
            horizon="scalp",
            entry_timing="dip_buy",
            entry_source="cio",
            trend_at_entry="UP",
            exit_reason="take_profit",
            mfe_pct=0.01,
            mae_pct=0.002,
            notional=10_000,
        ),
        ClosedTrade(
            pnl=-40.0,
            holding_minutes=90,
            horizon="scalp",
            entry_timing="chase",
            entry_source="cio",
            trend_at_entry="UP",
            exit_reason="session_flatten",
            mfe_pct=0.003,
            mae_pct=0.012,
            notional=10_000,
        ),
        ClosedTrade(
            pnl=-25.0,
            holding_minutes=2000,
            horizon="short",
            entry_timing="bounce",
            entry_source=SOURCE_INJECTED,
            trend_at_entry="SIDEWAYS",
            exit_reason="stop",
            mfe_pct=0.004,
            mae_pct=0.03,
            notional=20_000,
        ),
        ClosedTrade(pnl=-5.0, holding_minutes=10, horizon="day"),
    ]
    out = compute_entry_reason_expectancy(trades)
    dip = out["by_entry_reason"]["uptrend_dip"]
    assert dip["n"] == 1
    assert dip["win_pct"] == 1.0
    assert dip["tp_pct"] == 1.0
    chase = out["by_entry_reason"]["chase"]
    assert chase["n"] == 1
    assert chase["session_flatten_pct"] == 1.0
    injected = out["by_entry_reason"]["aggressive_injected"]
    assert injected["n"] == 1
    bounce = out["by_entry_reason"]["oversold_bounce"]
    assert bounce["n"] == 1
    assert bounce["stop_pct"] == 1.0
    assert out["cohorts"]["aggressive_injected_sideways"]["n"] == 1
    assert out["cohorts"]["short_oversold_bounce"]["n"] == 1
    assert out["cohorts"]["scalp_session_flatten"]["n"] == 1
    assert out["untagged"] == 1
    # 8 bps of 10k = $8; winner net 72
    assert modeled_cost(trades[0]) == 8.0
    assert dip["expectancy_after_costs"] == 72.0
