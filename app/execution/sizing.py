"""Deterministic position sizing."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class SizingInput:
    portfolio_equity: float
    risk_per_trade_pct: float
    entry_price: float
    stop_price: float
    max_position_pct: float
    available_buying_power: float
    min_cash_pct: float
    liquidity_cap_shares: float | None = None
    sector_cap_notional: float | None = None
    symbol_cap_notional: float | None = None
    existing_symbol_qty: float = 0.0
    fractionable: bool = True
    side: str = "buy"


@dataclass(slots=True)
class SizingResult:
    approved: bool
    quantity: float
    notional: float
    risk_amount: float
    stop_distance: float
    reason: str | None = None
    calculation_version: str = "sizing_v1"


def size_position(inp: SizingInput) -> SizingResult:
    stop_distance = abs(inp.entry_price - inp.stop_price)
    if stop_distance <= 0:
        return SizingResult(False, 0.0, 0.0, 0.0, 0.0, reason="stop_distance_invalid")
    # Direction check for long entries
    if inp.side.lower() == "buy" and inp.stop_price >= inp.entry_price:
        return SizingResult(False, 0.0, 0.0, 0.0, stop_distance, reason="stop_not_below_entry")
    if inp.side.lower() == "sell" and inp.stop_price <= inp.entry_price:
        return SizingResult(False, 0.0, 0.0, 0.0, stop_distance, reason="stop_not_above_entry_for_short")

    risk_amount = inp.portfolio_equity * (inp.risk_per_trade_pct / 100.0)
    raw_qty = risk_amount / stop_distance
    max_by_position = (inp.portfolio_equity * (inp.max_position_pct / 100.0)) / inp.entry_price
    qty = min(raw_qty, max_by_position)

    max_cash_spend = max(
        0.0,
        inp.available_buying_power
        - inp.portfolio_equity * (inp.min_cash_pct / 100.0) * 0.0,  # buying power already net
    )
    # Keep min cash: available buying power should respect cash floor via caller;
    # additionally cap notional by buying power.
    max_by_bp = inp.available_buying_power / inp.entry_price if inp.entry_price > 0 else 0.0
    qty = min(qty, max_by_bp)

    if inp.liquidity_cap_shares is not None:
        qty = min(qty, inp.liquidity_cap_shares)
    if inp.symbol_cap_notional is not None:
        qty = min(qty, inp.symbol_cap_notional / inp.entry_price)
    if inp.sector_cap_notional is not None:
        qty = min(qty, inp.sector_cap_notional / inp.entry_price)

    if not inp.fractionable:
        qty = float(int(qty))

    qty = max(0.0, qty)
    if qty <= 0:
        return SizingResult(False, 0.0, 0.0, risk_amount, stop_distance, reason="quantity_zero")

    notional = qty * inp.entry_price
    if notional > inp.available_buying_power + 1e-6 and inp.side.lower() == "buy":
        return SizingResult(False, 0.0, 0.0, risk_amount, stop_distance, reason="buying_power")

    return SizingResult(True, round(qty, 6), round(notional, 2), risk_amount, stop_distance)
