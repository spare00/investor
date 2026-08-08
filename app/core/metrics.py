"""Prometheus-compatible metrics registry."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, Info, generate_latest, CONTENT_TYPE_LATEST

APP_INFO = Info("investor_app", "Investor application info")
APP_INFO.info({"version": "0.12.0", "phase": "7"})

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

# Phase 7 operational metrics (low-cardinality labels only)
WORKFLOW_FAILURES = Counter(
    "investor_workflow_failures_total",
    "Workflow failures by kind",
    ["kind"],
)

AGENT_RUNS = Counter(
    "investor_agent_runs_total",
    "Agent runs by name and outcome",
    ["agent", "outcome"],
)

AGENT_FAILURES = Counter(
    "investor_agent_failures_total",
    "Agent failures by name",
    ["agent"],
)

AGENT_LATENCY = Histogram(
    "investor_agent_latency_seconds",
    "Agent wall time",
    ["agent"],
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120),
)

LLM_REPAIR_ATTEMPTS = Counter(
    "investor_llm_repair_attempts_total",
    "LLM JSON repair attempts",
    ["agent"],
)

PROVIDER_REQUESTS = Counter(
    "investor_provider_requests_total",
    "External provider requests",
    ["provider", "status"],
)

PROVIDER_FAILURES = Counter(
    "investor_provider_failures_total",
    "External provider failures",
    ["provider"],
)

PROVIDER_LATENCY = Histogram(
    "investor_provider_latency_seconds",
    "External provider latency",
    ["provider"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
)

DATA_STALE_EVENTS = Counter(
    "investor_data_stale_events_total",
    "Stale data events by source",
    ["source"],
)

ORDERS_REJECTED = Counter(
    "investor_orders_rejected_total",
    "Orders rejected before submit",
    ["reason"],
)

ORDERS_UNKNOWN = Counter(
    "investor_orders_unknown_total",
    "Orders in unknown broker state",
    ["broker"],
)

RECONCILIATION_ISSUES = Counter(
    "investor_reconciliation_issues_total",
    "Broker reconciliation issues",
    ["issue_type"],
)

ACTIVE_POSITIONS = Gauge("investor_active_positions", "Active open positions")
PORTFOLIO_VALUE = Gauge("investor_portfolio_value", "Current portfolio value")
DAILY_PNL = Gauge("investor_daily_pnl", "Current session daily PnL")
CURRENT_DRAWDOWN = Gauge("investor_current_drawdown", "Current drawdown fraction")
EMERGENCY_STOP_ACTIVE = Gauge(
    "investor_emergency_stop_active",
    "Emergency stop active (1=yes, 0=no)",
)
EVENT_QUEUE_DEPTH = Gauge("investor_event_queue_depth", "Pending event queue depth")

LLM_TOKENS_TODAY = Gauge(
    "investor_llm_tokens_today",
    "Billable LLM tokens recorded today (operator-local day)",
)
LLM_CALLS_TODAY = Gauge(
    "investor_llm_calls_today",
    "Billable LLM API calls recorded today (operator-local day)",
)
LLM_BUDGET_BLOCKED = Gauge(
    "investor_llm_budget_blocked",
    "LLM daily budget exhausted (1=yes, 0=no)",
)
LLM_BUDGET_EXCEEDED = Counter(
    "investor_llm_budget_exceeded_total",
    "LLM calls blocked by daily budget",
    ["reason"],
)


def trading_state_value(state: str) -> float:
    return {"active": 1.0, "paused": 0.5, "emergency_stop": 0.0}.get(state, -1.0)


def metrics_payload() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
