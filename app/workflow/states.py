"""Daily workflow state machine definitions."""

from __future__ import annotations

from enum import StrEnum


class DailyWorkflowState(StrEnum):
    NON_TRADING_DAY = "NON_TRADING_DAY"
    PREMARKET_PREPARATION = "PREMARKET_PREPARATION"
    PREMARKET_ANALYSIS = "PREMARKET_ANALYSIS"
    PREOPEN_REVALIDATION = "PREOPEN_REVALIDATION"
    MARKET_OPEN = "MARKET_OPEN"
    INTRADAY = "INTRADAY"
    CLOSING_WINDOW = "CLOSING_WINDOW"
    MARKET_CLOSED = "MARKET_CLOSED"
    POSTMARKET_REVIEW = "POSTMARKET_REVIEW"
    COMPLETED = "COMPLETED"
    PAUSED = "PAUSED"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    FAILED = "FAILED"


class WorkflowRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


class RevalidationResult(StrEnum):
    VALID = "VALID"
    VALID_WITH_RESTRICTIONS = "VALID_WITH_RESTRICTIONS"
    REANALYSIS_REQUIRED = "REANALYSIS_REQUIRED"
    NO_TRADE = "NO_TRADE"
    FAILED = "FAILED"


class IntradayEvalResult(StrEnum):
    NO_CHANGE = "NO_CHANGE"
    REANALYZE = "REANALYZE"
    RISK_REVIEW_REQUIRED = "RISK_REVIEW_REQUIRED"
    PAUSE_TRADING = "PAUSE_TRADING"
    EMERGENCY_STOP = "EMERGENCY_STOP"


class ClosingPolicy(StrEnum):
    KEEP_OVERNIGHT = "KEEP_OVERNIGHT"
    REDUCE_RISK = "REDUCE_RISK"
    CLOSE_INTRADAY_ONLY = "CLOSE_INTRADAY_ONLY"
    CLOSE_ALL = "CLOSE_ALL"
    MANUAL_REVIEW = "MANUAL_REVIEW"


# Allowed transitions (from -> frozenset[to]). PAUSED/EMERGENCY/FAILED are special.
ALLOWED_TRANSITIONS: dict[DailyWorkflowState, frozenset[DailyWorkflowState]] = {
    DailyWorkflowState.NON_TRADING_DAY: frozenset(
        {DailyWorkflowState.COMPLETED, DailyWorkflowState.FAILED, DailyWorkflowState.PAUSED}
    ),
    DailyWorkflowState.PREMARKET_PREPARATION: frozenset(
        {
            DailyWorkflowState.PREMARKET_ANALYSIS,
            DailyWorkflowState.NON_TRADING_DAY,
            DailyWorkflowState.PAUSED,
            DailyWorkflowState.EMERGENCY_STOP,
            DailyWorkflowState.FAILED,
        }
    ),
    DailyWorkflowState.PREMARKET_ANALYSIS: frozenset(
        {
            DailyWorkflowState.PREOPEN_REVALIDATION,
            DailyWorkflowState.PAUSED,
            DailyWorkflowState.EMERGENCY_STOP,
            DailyWorkflowState.FAILED,
        }
    ),
    DailyWorkflowState.PREOPEN_REVALIDATION: frozenset(
        {
            DailyWorkflowState.MARKET_OPEN,
            DailyWorkflowState.PREMARKET_ANALYSIS,  # reanalysis
            DailyWorkflowState.PAUSED,
            DailyWorkflowState.EMERGENCY_STOP,
            DailyWorkflowState.FAILED,
        }
    ),
    DailyWorkflowState.MARKET_OPEN: frozenset(
        {
            DailyWorkflowState.INTRADAY,
            DailyWorkflowState.CLOSING_WINDOW,
            DailyWorkflowState.PAUSED,
            DailyWorkflowState.EMERGENCY_STOP,
            DailyWorkflowState.FAILED,
        }
    ),
    DailyWorkflowState.INTRADAY: frozenset(
        {
            DailyWorkflowState.INTRADAY,
            DailyWorkflowState.CLOSING_WINDOW,
            DailyWorkflowState.PAUSED,
            DailyWorkflowState.EMERGENCY_STOP,
            DailyWorkflowState.FAILED,
        }
    ),
    DailyWorkflowState.CLOSING_WINDOW: frozenset(
        {
            DailyWorkflowState.MARKET_CLOSED,
            DailyWorkflowState.PAUSED,
            DailyWorkflowState.EMERGENCY_STOP,
            DailyWorkflowState.FAILED,
        }
    ),
    DailyWorkflowState.MARKET_CLOSED: frozenset(
        {
            DailyWorkflowState.POSTMARKET_REVIEW,
            DailyWorkflowState.PAUSED,
            DailyWorkflowState.EMERGENCY_STOP,
            DailyWorkflowState.FAILED,
        }
    ),
    DailyWorkflowState.POSTMARKET_REVIEW: frozenset(
        {
            DailyWorkflowState.COMPLETED,
            DailyWorkflowState.PAUSED,
            DailyWorkflowState.EMERGENCY_STOP,
            DailyWorkflowState.FAILED,
        }
    ),
    DailyWorkflowState.COMPLETED: frozenset(),
    DailyWorkflowState.PAUSED: frozenset(
        {
            # Resume returns to a caller-specified prior operational state via metadata
            DailyWorkflowState.PREMARKET_PREPARATION,
            DailyWorkflowState.PREMARKET_ANALYSIS,
            DailyWorkflowState.PREOPEN_REVALIDATION,
            DailyWorkflowState.MARKET_OPEN,
            DailyWorkflowState.INTRADAY,
            DailyWorkflowState.CLOSING_WINDOW,
            DailyWorkflowState.MARKET_CLOSED,
            DailyWorkflowState.POSTMARKET_REVIEW,
            DailyWorkflowState.EMERGENCY_STOP,
            DailyWorkflowState.FAILED,
        }
    ),
    DailyWorkflowState.EMERGENCY_STOP: frozenset(
        {DailyWorkflowState.PAUSED, DailyWorkflowState.FAILED}
    ),
    DailyWorkflowState.FAILED: frozenset({DailyWorkflowState.PAUSED}),
}


def assert_transition_allowed(from_state: DailyWorkflowState, to_state: DailyWorkflowState) -> None:
    allowed = ALLOWED_TRANSITIONS.get(from_state, frozenset())
    if to_state not in allowed:
        raise ValueError(f"illegal_transition:{from_state.value}->{to_state.value}")


# Broker orders are never allowed from the Phase 3 daily state machine.
BROKER_ORDERS_ALLOWED: dict[DailyWorkflowState, bool] = {
    state: False for state in DailyWorkflowState
}
