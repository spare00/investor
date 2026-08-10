"""Canonical broker models (Phase 5)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator


class BrokerEnvironment(StrEnum):
    PAPER = "paper"
    LIVE = "live"
    MOCK = "mock"


class IntentType(StrEnum):
    OPEN_LONG = "OPEN_LONG"
    ADD_LONG = "ADD_LONG"
    REDUCE_LONG = "REDUCE_LONG"
    CLOSE_LONG = "CLOSE_LONG"
    OPEN_SHORT = "OPEN_SHORT"
    ADD_SHORT = "ADD_SHORT"
    REDUCE_SHORT = "REDUCE_SHORT"
    CLOSE_SHORT = "CLOSE_SHORT"
    HEDGE = "HEDGE"
    CANCEL_PENDING = "CANCEL_PENDING"
    NO_ACTION = "NO_ACTION"


class IntentStatus(StrEnum):
    CREATED = "CREATED"
    VALIDATING = "VALIDATING"
    RISK_REJECTED = "RISK_REJECTED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ApprovalStatus(StrEnum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    AUTO_APPROVED = "AUTO_APPROVED"


class PretradeStatus(StrEnum):
    APPROVED = "APPROVED"
    APPROVED_WITH_REDUCTION = "APPROVED_WITH_REDUCTION"
    REQUIRES_MANUAL_APPROVAL = "REQUIRES_MANUAL_APPROVAL"
    REJECTED = "REJECTED"
    SYSTEM_BLOCKED = "SYSTEM_BLOCKED"


class InternalOrderState(StrEnum):
    CREATED = "CREATED"
    VALIDATING = "VALIDATING"
    RISK_REJECTED = "RISK_REJECTED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REPLACE_PENDING = "REPLACE_PENDING"
    REPLACED = "REPLACED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class ReconciliationResult(StrEnum):
    IN_SYNC = "IN_SYNC"
    MINOR_DRIFT = "MINOR_DRIFT"
    MATERIAL_DRIFT = "MATERIAL_DRIFT"
    BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE"
    LOCAL_STATE_INVALID = "LOCAL_STATE_INVALID"


class BrokerAccount(BaseModel):
    account_id_reference: str
    environment: BrokerEnvironment
    currency: str = "USD"
    equity: float
    cash: float
    buying_power: float
    portfolio_value: float
    long_market_value: float = 0.0
    short_market_value: float = 0.0
    initial_margin: float | None = None
    maintenance_margin: float | None = None
    daytrade_count: int = 0
    pattern_day_trader: bool = False
    trading_blocked: bool = False
    transfers_blocked: bool = False
    account_blocked: bool = False
    as_of: datetime
    source: str


class BrokerClock(BaseModel):
    is_open: bool
    timestamp: datetime
    next_open: datetime | None = None
    next_close: datetime | None = None
    source: str


class BrokerAsset(BaseModel):
    symbol: str
    tradable: bool
    fractionable: bool = False
    shortable: bool = False
    easy_to_borrow: bool = False
    exchange: str | None = None
    asset_class: str = "us_equity"
    status: str = "active"


class BrokerPosition(BaseModel):
    symbol: str
    quantity: float
    available_quantity: float | None = None
    side: str  # long | short
    market_value: float
    cost_basis: float | None = None
    average_entry_price: float | None = None
    current_price: float | None = None
    unrealized_pl: float | None = None
    unrealized_pl_pct: float | None = None
    asset_class: str = "us_equity"
    exchange: str | None = None
    currency: str | None = None
    as_of: datetime
    source: str
    con_id: int | None = None


class BrokerOrderRequest(BaseModel):
    client_order_id: str
    decision_id: str | None = None
    symbol: str
    side: str
    quantity: float | None = None
    notional: float | None = None
    order_type: str = "limit"
    time_in_force: str = "day"
    limit_price: float | None = None
    stop_price: float | None = None
    trail_percent: float | None = None
    extended_hours: bool = False
    position_intent: str | None = None
    strategy_reference: str | None = None
    risk_check_id: str | None = None
    approval_id: str | None = None
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def _qty_xor_notional(self) -> BrokerOrderRequest:
        if self.quantity is not None and self.notional is not None:
            raise ValueError("quantity_and_notional_mutually_exclusive")
        if self.quantity is None and self.notional is None:
            raise ValueError("quantity_or_notional_required")
        return self


class BrokerOrder(BaseModel):
    broker_order_id: str
    client_order_id: str | None = None
    decision_id: str | None = None
    symbol: str
    side: str
    quantity: float
    filled_quantity: float = 0.0
    remaining_quantity: float | None = None
    order_type: str
    time_in_force: str = "day"
    limit_price: float | None = None
    stop_price: float | None = None
    average_fill_price: float | None = None
    status: str
    submitted_at: datetime | None = None
    filled_at: datetime | None = None
    cancelled_at: datetime | None = None
    rejected_at: datetime | None = None
    failure_reason: str | None = None
    broker_raw_status: str | None = None
    last_updated_at: datetime | None = None


class BrokerExecution(BaseModel):
    execution_id: str
    broker_order_id: str
    symbol: str
    quantity: float
    price: float
    executed_at: datetime
    fee: float | None = None


class BrokerHealth(BaseModel):
    healthy: bool
    provider: str
    environment: str
    connected: bool
    message: str | None = None
    as_of: datetime


class BrokerCapabilities(BaseModel):
    provider: str
    supports_replace: bool = False
    supports_bracket: bool = False
    supports_fractional: bool = True
    supports_short: bool = False
    supports_extended_hours: bool = False
    supports_streaming: bool = False
    is_mock: bool = False


class ExitPolicy(BaseModel):
    stop_loss: float | None = None
    invalidation_condition: str | None = None
    take_profit_policy: str | None = None
    max_holding_time_minutes: int | None = None
    closing_policy: str = "CLOSE_INTRADAY_ONLY"
    overnight_allowed: bool = False
    protection_submitted: bool = False


ALLOWED_ORDER_TRANSITIONS: dict[InternalOrderState, frozenset[InternalOrderState]] = {
    InternalOrderState.CREATED: frozenset(
        {
            InternalOrderState.VALIDATING,
            InternalOrderState.CANCELLED,
            InternalOrderState.FAILED,
        }
    ),
    InternalOrderState.VALIDATING: frozenset(
        {
            InternalOrderState.RISK_REJECTED,
            InternalOrderState.PENDING_APPROVAL,
            InternalOrderState.APPROVED,
            InternalOrderState.FAILED,
        }
    ),
    InternalOrderState.RISK_REJECTED: frozenset(),
    InternalOrderState.PENDING_APPROVAL: frozenset(
        {
            InternalOrderState.APPROVED,
            InternalOrderState.REJECTED,
            InternalOrderState.EXPIRED,
            InternalOrderState.CANCELLED,
        }
    ),
    InternalOrderState.APPROVED: frozenset(
        {
            InternalOrderState.SUBMITTING,
            InternalOrderState.EXPIRED,
            InternalOrderState.CANCELLED,
            InternalOrderState.FAILED,
        }
    ),
    InternalOrderState.SUBMITTING: frozenset(
        {
            InternalOrderState.SUBMITTED,
            InternalOrderState.UNKNOWN,
            InternalOrderState.FAILED,
            InternalOrderState.RECONCILIATION_REQUIRED,
        }
    ),
    InternalOrderState.SUBMITTED: frozenset(
        {
            InternalOrderState.ACCEPTED,
            InternalOrderState.PARTIALLY_FILLED,
            InternalOrderState.FILLED,
            InternalOrderState.CANCEL_PENDING,
            InternalOrderState.REJECTED,
            InternalOrderState.EXPIRED,
            InternalOrderState.UNKNOWN,
            InternalOrderState.RECONCILIATION_REQUIRED,
        }
    ),
    InternalOrderState.ACCEPTED: frozenset(
        {
            InternalOrderState.PARTIALLY_FILLED,
            InternalOrderState.FILLED,
            InternalOrderState.CANCEL_PENDING,
            InternalOrderState.REPLACE_PENDING,
            InternalOrderState.CANCELLED,
            InternalOrderState.EXPIRED,
            InternalOrderState.REJECTED,
        }
    ),
    InternalOrderState.PARTIALLY_FILLED: frozenset(
        {
            InternalOrderState.FILLED,
            InternalOrderState.CANCEL_PENDING,
            InternalOrderState.REPLACE_PENDING,
            InternalOrderState.CANCELLED,
        }
    ),
    InternalOrderState.FILLED: frozenset(),
    InternalOrderState.CANCEL_PENDING: frozenset(
        {
            InternalOrderState.CANCELLED,
            InternalOrderState.FILLED,
            InternalOrderState.PARTIALLY_FILLED,
            InternalOrderState.RECONCILIATION_REQUIRED,
        }
    ),
    InternalOrderState.CANCELLED: frozenset(),
    InternalOrderState.REPLACE_PENDING: frozenset(
        {
            InternalOrderState.REPLACED,
            InternalOrderState.FILLED,
            InternalOrderState.RECONCILIATION_REQUIRED,
        }
    ),
    InternalOrderState.REPLACED: frozenset(),
    InternalOrderState.REJECTED: frozenset(),
    InternalOrderState.EXPIRED: frozenset(),
    InternalOrderState.FAILED: frozenset({InternalOrderState.RECONCILIATION_REQUIRED}),
    InternalOrderState.UNKNOWN: frozenset(
        {
            InternalOrderState.SUBMITTED,
            InternalOrderState.ACCEPTED,
            InternalOrderState.FILLED,
            InternalOrderState.CANCELLED,
            InternalOrderState.REJECTED,
            InternalOrderState.RECONCILIATION_REQUIRED,
            InternalOrderState.FAILED,
        }
    ),
    InternalOrderState.RECONCILIATION_REQUIRED: frozenset(
        {
            InternalOrderState.SUBMITTED,
            InternalOrderState.ACCEPTED,
            InternalOrderState.FILLED,
            InternalOrderState.CANCELLED,
            InternalOrderState.REJECTED,
            InternalOrderState.FAILED,
            InternalOrderState.UNKNOWN,
        }
    ),
}


def assert_order_transition(frm: InternalOrderState, to: InternalOrderState) -> None:
    allowed = ALLOWED_ORDER_TRANSITIONS.get(frm, frozenset())
    if to not in allowed:
        raise ValueError(f"illegal_order_transition:{frm.value}->{to.value}")


def redact_account_id(raw_id: str | None) -> str:
    if not raw_id:
        return "acct_unknown"
    if len(raw_id) <= 4:
        return f"acct_***{raw_id}"
    return f"acct_***{raw_id[-4:]}"
