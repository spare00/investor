"""Prometheus-compatible metrics registry."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, Info, generate_latest, CONTENT_TYPE_LATEST

APP_INFO = Info("investor_app", "Investor application info")
APP_INFO.info({"version": "0.7.0", "phase": "7"})

WORKFLOW_RUNS = Counter(
    "investor_workflow_runs_total",
    "Workflow runs by kind and outcome",
    ["kind", "outcome"],
)

ORDERS_SUBMITTED = Counter(
    "investor_orders_submitted_total",
    "Orders submitted to broker",
    ["symbol", "side", "status"],
)

ORDERS_BLOCKED = Counter(
    "investor_orders_blocked_total",
    "Orders blocked before broker submit",
    ["reason"],
)

HARD_VETOES = Counter(
    "investor_hard_vetoes_total",
    "Hard veto occurrences",
    ["code"],
)

PORTFOLIO_EQUITY = Gauge("investor_portfolio_equity", "Latest portfolio equity")
PORTFOLIO_CASH = Gauge("investor_portfolio_cash", "Latest portfolio cash")
PORTFOLIO_DRAWDOWN_PCT = Gauge(
    "investor_portfolio_drawdown_pct", "Latest portfolio drawdown percent"
)
OPEN_POSITIONS = Gauge("investor_open_positions", "Open position count")
TRADING_STATE = Gauge(
    "investor_trading_state",
    "Trading control state (1=active, 0.5=paused, 0=emergency)",
)

WORKFLOW_DURATION = Histogram(
    "investor_workflow_duration_seconds",
    "Workflow wall time",
    ["kind"],
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300),
)


def trading_state_value(state: str) -> float:
    return {"active": 1.0, "paused": 0.5, "emergency_stop": 0.0}.get(state, -1.0)


def metrics_payload() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
