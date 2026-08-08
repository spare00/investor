"""Deterministic performance calculation engines."""

from app.performance.agent_eval import (
    AgentPrediction,
    Direction,
    evaluate_agents,
    evaluate_agents_grouped,
    score_devil_advocate,
    score_risk_manager,
)
from app.performance.benchmarks import (
    FIXTURE_BENCHMARKS,
    aligned_benchmark_returns,
    load_and_align,
    load_benchmark_series,
)
from app.performance.calibration import (
    bucket_accuracy,
    calibration_gap,
    expected_calibration_error,
)
from app.performance.decision_eval import DecisionAction, evaluate_decision
from app.performance.drawdown import (
    DrawdownPeriod,
    DrawdownStatus,
    compute_drawdowns,
    current_drawdown,
    max_drawdown,
)
from app.performance.execution_quality import compute_execution_quality
from app.performance.mae_mfe import MaeMfeResult, compute_mae_mfe
from app.performance.operational import aggregate_operational_kpis
from app.performance.providers import compute_provider_reliability
from app.performance.returns import (
    active_return,
    annualized_return,
    cumulative_return,
    daily_returns,
    excess_return,
    money_weighted_return,
    portfolio_absolute_return,
    simple_return,
    time_weighted_return,
)
from app.performance.risk import (
    alpha,
    annualized_volatility,
    beta,
    cagr,
    calmar_ratio,
    downside_deviation,
    expected_shortfall,
    historical_var,
    information_ratio,
    sharpe_ratio,
    sortino_ratio,
    tracking_error,
)
from app.performance.service import PerformanceService
from app.performance.trades import ClosedTrade, compute_trade_metrics, group_trade_metrics_by_horizon
from app.performance.types import (
    ANNUALIZATION_FACTOR,
    CALCULATION_VERSION,
    DEFAULT_MIN_OBS,
    MetricResult,
    MetricStatus,
    metric_result,
)
from app.performance.valuation import (
    PortfolioValuation,
    build_portfolio_valuation,
    valuation_dedup_key,
)

__all__ = [
    "ANNUALIZATION_FACTOR",
    "CALCULATION_VERSION",
    "AgentPrediction",
    "ClosedTrade",
    "DEFAULT_MIN_OBS",
    "DecisionAction",
    "Direction",
    "DrawdownPeriod",
    "DrawdownStatus",
    "FIXTURE_BENCHMARKS",
    "MaeMfeResult",
    "MetricResult",
    "MetricStatus",
    "PerformanceService",
    "PortfolioValuation",
    "active_return",
    "aggregate_operational_kpis",
    "aligned_benchmark_returns",
    "alpha",
    "annualized_return",
    "annualized_volatility",
    "beta",
    "bucket_accuracy",
    "build_portfolio_valuation",
    "cagr",
    "calibration_gap",
    "calmar_ratio",
    "compute_drawdowns",
    "compute_execution_quality",
    "compute_mae_mfe",
    "compute_provider_reliability",
    "compute_trade_metrics",
    "group_trade_metrics_by_horizon",
    "cumulative_return",
    "current_drawdown",
    "daily_returns",
    "downside_deviation",
    "evaluate_agents",
    "evaluate_agents_grouped",
    "evaluate_decision",
    "excess_return",
    "expected_calibration_error",
    "expected_shortfall",
    "historical_var",
    "information_ratio",
    "load_and_align",
    "load_benchmark_series",
    "max_drawdown",
    "metric_result",
    "money_weighted_return",
    "portfolio_absolute_return",
    "score_devil_advocate",
    "score_risk_manager",
    "sharpe_ratio",
    "simple_return",
    "sortino_ratio",
    "time_weighted_return",
    "tracking_error",
    "valuation_dedup_key",
]
