"""Deterministic execution policy (order type / sizing adjustments)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(slots=True)
class ExecutionPolicyInput:
    symbol: str
    side: str
    quantity: float
    entry_price: float | None
    stop_price: float | None
    spread_bps: float | None
    quote_age_seconds: float | None
    liquidity_shares: float | None
    market_open: bool
    minutes_to_close: float | None
    urgency: str = "normal"  # low | normal | high
    preferred_order_type: str = "limit"
    max_spread_bps: float = 50.0
    max_quote_age_seconds: float = 600.0
    extended_hours_allowed: bool = False


@dataclass(slots=True)
class ExecutionPolicyResult:
    allowed: bool
    order_type: str
    quantity: float
    limit_price: float | None
    stop_price: float | None
    time_in_force: str
    extended_hours: bool
    max_slippage_bps: float
    cancel_replace_policy: str
    reasons: list[str]


def select_execution(inp: ExecutionPolicyInput) -> ExecutionPolicyResult:
    reasons: list[str] = []
    qty = inp.quantity
    if qty <= 0:
        return ExecutionPolicyResult(
            False, "limit", 0.0, None, inp.stop_price, "day", False, 30.0, "WAIT", ["quantity_zero"]
        )
    if not inp.market_open and not inp.extended_hours_allowed:
        return ExecutionPolicyResult(
            False, "limit", 0.0, None, inp.stop_price, "day", False, 30.0, "WAIT", ["market_closed"]
        )
    if inp.quote_age_seconds is not None and inp.quote_age_seconds > inp.max_quote_age_seconds:
        return ExecutionPolicyResult(
            False, "limit", 0.0, None, inp.stop_price, "day", False, 30.0, "WAIT", ["stale_quote"]
        )
    if inp.stop_price is None and inp.side.lower() == "buy":
        return ExecutionPolicyResult(
            False, "limit", 0.0, None, None, "day", False, 30.0, "WAIT", ["stop_required"]
        )
    if inp.minutes_to_close is not None and inp.minutes_to_close < 15 and inp.side.lower() == "buy":
        return ExecutionPolicyResult(
            False, "limit", 0.0, None, inp.stop_price, "day", False, 30.0, "WAIT", ["near_close_block"]
        )

    order_type = inp.preferred_order_type or "limit"
    if inp.spread_bps is not None and inp.spread_bps > inp.max_spread_bps:
        reasons.append("spread_wide_force_limit")
        order_type = "limit"
    if order_type == "market" and inp.spread_bps is not None and inp.spread_bps > 20:
        reasons.append("market_forbidden_wide_spread")
        order_type = "limit"

    if inp.liquidity_shares is not None and qty > inp.liquidity_shares * 0.05:
        qty = max(0.0, inp.liquidity_shares * 0.05)
        reasons.append("liquidity_cap")

    limit_price = inp.entry_price
    if order_type == "limit" and limit_price is None:
        return ExecutionPolicyResult(
            False, "limit", 0.0, None, inp.stop_price, "day", False, 30.0, "WAIT", ["limit_price_required"]
        )

    if qty <= 0:
        return ExecutionPolicyResult(
            False, order_type, 0.0, limit_price, inp.stop_price, "day", False, 30.0, "WAIT", reasons + ["qty_zero"]
        )

    cancel_policy = "WAIT"
    if inp.urgency == "high":
        cancel_policy = "REPLACE_REMAINDER"
    elif inp.minutes_to_close is not None and inp.minutes_to_close < 30:
        cancel_policy = "CANCEL_REMAINDER"

    return ExecutionPolicyResult(
        allowed=True,
        order_type=order_type,
        quantity=round(qty, 6),
        limit_price=limit_price,
        stop_price=inp.stop_price,
        time_in_force="day",
        extended_hours=False,
        max_slippage_bps=30.0,
        cancel_replace_policy=cancel_policy,
        reasons=reasons or ["ok"],
    )


def as_of_iso() -> str:
    return datetime.now(UTC).isoformat()
