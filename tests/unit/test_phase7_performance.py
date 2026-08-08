"""Phase 7 deterministic performance engine unit tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.performance.agent_eval import (
    AgentPrediction,
    Direction,
    evaluate_agents,
    score_devil_advocate,
    score_risk_manager,
)
from app.performance.calibration import calibration_gap, expected_calibration_error
from app.performance.decision_eval import DecisionAction, evaluate_decision
from app.performance.drawdown import DrawdownStatus, compute_drawdowns, current_drawdown, max_drawdown
from app.performance.execution_quality import compute_execution_quality
from app.performance.mae_mfe import compute_mae_mfe
from app.performance.providers import compute_provider_reliability
from app.performance.returns import (
    active_return,
    excess_return,
    money_weighted_return,
    simple_return,
    time_weighted_return,
)
from app.performance.risk import (
    alpha,
    annualized_volatility,
    beta,
    cagr,
    historical_var,
    sharpe_ratio,
    sortino_ratio,
    tracking_error,
)
from app.performance.trades import ClosedTrade, compute_trade_metrics
from app.performance.types import MetricStatus
from app.performance.valuation import build_portfolio_valuation, valuation_dedup_key


def test_simple_and_twr() -> None:
    assert simple_return(100.0, 110.0) == pytest.approx(0.1)
    twr = time_weighted_return([0.1, -0.05, 0.02])
    assert twr.status == MetricStatus.AVAILABLE
    assert twr.value == pytest.approx((1.1 * 0.95 * 1.02) - 1.0)


def test_mwr_insufficient_without_cashflows() -> None:
    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    t1 = datetime(2024, 2, 1, tzinfo=UTC)
    mwr = money_weighted_return(start_value=100.0, end_value=110.0, period_start=t0, period_end=t1, cashflows=None)
    # Without external cashflows, MWR may still compute or mark insufficient — never invent flows
    assert mwr.status in {
        MetricStatus.INSUFFICIENT_DATA,
        MetricStatus.AVAILABLE,
        MetricStatus.UNRELIABLE,
    }


def test_benchmark_missing_relative_unavailable() -> None:
    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    port = [(t0, 0.01), (t0 + timedelta(days=1), 0.02)]
    er = excess_return(port, [])
    assert er.status == MetricStatus.UNAVAILABLE
    ar = active_return(port, [(t0, 0.01)])  # length/date mismatch → unavailable
    assert ar.status in {MetricStatus.UNAVAILABLE, MetricStatus.INSUFFICIENT_DATA}


def test_drawdown_active_and_recovered() -> None:
    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    curve = [
        (t0, 100.0),
        (t0 + timedelta(days=1), 110.0),
        (t0 + timedelta(days=2), 90.0),
        (t0 + timedelta(days=3), 95.0),
        (t0 + timedelta(days=4), 110.0),
        (t0 + timedelta(days=5), 100.0),
    ]
    dds = compute_drawdowns(curve)
    assert any(d.status in {DrawdownStatus.RECOVERED, DrawdownStatus.ACTIVE} for d in dds)
    mdd = max_drawdown(curve)
    assert mdd.status == MetricStatus.AVAILABLE
    assert mdd.value is not None and abs(mdd.value) > 0
    cur = current_drawdown(curve)
    assert cur.status == MetricStatus.AVAILABLE


def test_risk_insufficient_and_zero_vol() -> None:
    s = sharpe_ratio([0.01], risk_free_rate=0.0, min_obs=20)
    assert s.status == MetricStatus.INSUFFICIENT_DATA
    zero = sharpe_ratio([0.0] * 30, risk_free_rate=0.0, min_obs=20)
    assert zero.status in {MetricStatus.UNRELIABLE, MetricStatus.UNAVAILABLE}
    vol = annualized_volatility([0.01] * 30, min_obs=20)
    assert vol.status == MetricStatus.AVAILABLE
    c = cagr(100.0, 110.0, years=1.0)
    assert c.status == MetricStatus.AVAILABLE


def test_sortino_beta_alpha_te_var() -> None:
    rets = [0.01, -0.02, 0.015, -0.005, 0.01] * 10
    bench = [0.008, -0.01, 0.01, 0.0, 0.009] * 10
    assert sortino_ratio(rets, risk_free_rate=0.0, min_obs=20).status == MetricStatus.AVAILABLE
    assert beta(rets, bench, min_obs=20).status == MetricStatus.AVAILABLE
    assert alpha(rets, bench, risk_free_rate=0.0, min_obs=20).status == MetricStatus.AVAILABLE
    assert tracking_error(rets, bench, min_obs=20).status == MetricStatus.AVAILABLE
    assert historical_var(rets, min_obs=20).status == MetricStatus.AVAILABLE


def test_trade_metrics_lifecycle_unit() -> None:
    trades = [
        ClosedTrade(pnl=100.0, holding_minutes=60, risk_amount=50.0, fees=1.0),
        ClosedTrade(pnl=-40.0, holding_minutes=30, risk_amount=50.0, fees=1.0),
        ClosedTrade(pnl=0.0, holding_minutes=10, risk_amount=20.0, fees=0.5),
        ClosedTrade(pnl=80.0, holding_minutes=120, risk_amount=40.0, fees=1.0),
    ]
    m = compute_trade_metrics(trades)
    assert m["trade_count"] == 4
    assert m["win_rate"].value == pytest.approx(0.5)


def test_trade_metrics_by_horizon() -> None:
    from app.performance.trades import group_trade_metrics_by_horizon

    trades = [
        ClosedTrade(pnl=50.0, holding_minutes=30, horizon="scalp", symbol="QQQ"),
        ClosedTrade(pnl=-20.0, holding_minutes=40, horizon="scalp", symbol="SPY"),
        ClosedTrade(pnl=100.0, holding_minutes=2000, horizon="medium", symbol="MSFT"),
        ClosedTrade(pnl=-10.0, holding_minutes=15, horizon=None, symbol="XYZ"),
    ]
    by_h = group_trade_metrics_by_horizon(trades)
    assert by_h["scalp"]["trade_count"] == 2
    assert by_h["scalp"]["win_rate"].value == pytest.approx(0.5)
    assert by_h["medium"]["trade_count"] == 1
    assert by_h["day"]["trade_count"] == 0
    assert by_h["unknown"]["trade_count"] == 1


def test_mae_mfe_long_and_insufficient() -> None:
    r = compute_mae_mfe(
        entry_price=100.0,
        exit_price=105.0,
        stop_distance=2.0,
        prices_during_hold=[(0, 100.0), (1, 98.0), (2, 103.0), (3, 107.0), (4, 105.0)],
    )
    assert r.mae.status == MetricStatus.AVAILABLE
    assert r.mfe.value is not None and r.mfe.value > 0
    insuff = compute_mae_mfe(
        entry_price=100.0,
        exit_price=105.0,
        stop_distance=2.0,
        prices_during_hold=[],
    )
    assert insuff.mae.status == MetricStatus.INSUFFICIENT_DATA


def test_execution_quality_missing_ts() -> None:
    eq = compute_execution_quality(
        {
            "decision_price": 100.0,
            "avg_fill_price": 100.5,
            "submitted_qty": 10,
            "filled_qty": 10,
        }
    )
    assert "slippage_bps" in eq or "implementation_shortfall" in eq or "fill_rate" in eq


def test_decision_eval_buy_and_no_trade() -> None:
    buy = evaluate_decision(
        decision_price=100.0,
        action=DecisionAction.BUY,
        horizon_price=105.0,
        benchmark_return=0.01,
    )
    assert buy["realized_return"].value == pytest.approx(0.05)
    nt = evaluate_decision(
        decision_price=100.0,
        action=DecisionAction.NO_TRADE,
        horizon_price=90.0,
        benchmark_return=-0.05,
    )
    assert "abstention_quality" in nt


def test_agent_calibration_and_roles() -> None:
    preds = [
        AgentPrediction(predicted_direction=Direction.BULLISH, confidence=0.8, actual_return=0.02, abstained=False),
        AgentPrediction(predicted_direction=Direction.BULLISH, confidence=0.7, actual_return=-0.01, abstained=False),
        AgentPrediction(predicted_direction=Direction.BEARISH, confidence=0.6, actual_return=-0.02, abstained=False),
        AgentPrediction(predicted_direction=Direction.BULLISH, confidence=0.55, actual_return=0.01, abstained=False),
        AgentPrediction(predicted_direction=Direction.ABSTAIN, confidence=0.5, actual_return=0.03, abstained=True),
    ]
    summary = evaluate_agents(preds)
    assert summary["directional_accuracy"].status == MetricStatus.AVAILABLE
    rm = score_risk_manager(
        blocked_trade_pnl=-500.0,
        portfolio_pnl_without_block=-500.0,
        loss_avoided=True,
    )
    assert rm.status == MetricStatus.AVAILABLE
    da = score_devil_advocate(
        counterargument_valid=True,
        thesis_failed=True,
        dissent_recorded=True,
    )
    assert da.value == pytest.approx(1.0)


def test_agent_eval_by_horizon_and_agent() -> None:
    from app.performance.agent_eval import evaluate_agents_grouped

    preds = [
        AgentPrediction(
            predicted_direction=Direction.BULLISH,
            confidence=0.8,
            actual_return=0.02,
            universe_horizon="scalp",
            agent_name="cio",
        ),
        AgentPrediction(
            predicted_direction=Direction.BULLISH,
            confidence=0.7,
            actual_return=-0.01,
            universe_horizon="scalp",
            agent_name="quant_strategist",
        ),
        AgentPrediction(
            predicted_direction=Direction.BEARISH,
            confidence=0.6,
            actual_return=-0.02,
            universe_horizon="medium",
            agent_name="cio",
        ),
        AgentPrediction(
            predicted_direction=Direction.BULLISH,
            confidence=0.9,
            actual_return=0.05,
            universe_horizon=None,
            agent_name="cio",
        ),
    ]
    by_h = evaluate_agents_grouped(preds, by="universe_horizon")
    assert by_h["scalp"]["prediction_count"] == 2
    assert by_h["medium"]["prediction_count"] == 1
    assert by_h["unknown"]["prediction_count"] == 1
    assert by_h["day"]["prediction_count"] == 0
    by_a = evaluate_agents_grouped(preds, by="agent_name")
    assert by_a["cio"]["prediction_count"] == 3
    assert by_a["quant_strategist"]["prediction_count"] == 1


def test_calibration_min_sample() -> None:
    pairs = [(0.9, 1.0)] * 5
    ece = expected_calibration_error(pairs, min_sample_size=30)
    assert ece.status == MetricStatus.INSUFFICIENT_DATA
    gap = calibration_gap(pairs, min_sample_size=30)
    assert gap.status == MetricStatus.INSUFFICIENT_DATA


def test_provider_reliability() -> None:
    stats = {
        "total_requests": 100,
        "successes": 90,
        "timeouts": 5,
        "errors": 5,
        "latencies_ms": [10, 20, 30, 40, 100],
        "freshness_seconds": 12,
    }
    rel = compute_provider_reliability(stats)
    assert rel["success_rate"].value == pytest.approx(0.9)


def test_valuation_dedup() -> None:
    as_of = datetime(2024, 1, 2, tzinfo=UTC)
    v = build_portfolio_valuation(
        portfolio_id="default",
        as_of=as_of,
        valuation_kind="MARKET_CLOSE",
        cash=10_000.0,
        positions=[{"symbol": "SPY", "quantity": 10, "price": 500.0, "side": "long"}],
        fees=1.0,
        slippage=0.5,
        benchmarks={"SPY": 500.0},
        source_snapshot_ids=["s1"],
    )
    assert v["equity"] == pytest.approx(15_000.0)
    key = valuation_dedup_key("default", as_of, "MARKET_CLOSE")
    assert "default" in key
