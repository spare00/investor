"""Venue-specific order shaping.

ASX (and IBKR paper ASX) does not fill native MKT the way US SMART does.
Convert market and ordinary limit orders to an aggressive limit so flatten
exits actually trade.

Resting stop and stop-limit orders are not part of that conversion. Rewriting
a protective stop into a limit through the current quote sells immediately.
This module refuses that rewrite; the broker adapter must send the trigger
or surface a rejection.
"""

from __future__ import annotations

import math

from app.brokers.pricing import round_equity_price

# Through-the-market offset. 0.8% was not enough once the snapshot was delayed
# or missing; IBKR still collars ~10%, so 2.5% stays inside the band.
_AU_SLIP_PCT = 0.025
# Hard-stop / force-close: punch through a falling ASX print. Stay under ~10% collar.
_AU_FLATTEN_SLIP_PCT = 0.08
_AU_MIN_SLIP = 0.02
# IBKR unset / NaN ticks often show up as DBL_MAX or 0.
_MAX_SANE_EQUITY_PX = 1_000_000.0


# canonical_order_type folds STP / STP LMT / TRAIL onto these names.
_RESTING_STOP_TYPES = frozenset({"stop", "stop_limit", "trailing_stop"})


def is_resting_stop(order_type: str | None) -> bool:
    """True when the order must keep a trigger instead of becoming a limit."""
    text = str(order_type or "").strip()
    if not text:
        return False
    from app.brokers.models import canonical_order_type

    return canonical_order_type(text) in _RESTING_STOP_TYPES


def uses_marketable_limit(venue: str | None, exchange: str | None = None) -> bool:
    if str(venue or "").strip().upper() == "AU":
        return True
    return str(exchange or "").strip().upper() in {"ASX", "ASX2"}


def is_sane_equity_price(value: float | None) -> bool:
    if value is None:
        return False
    try:
        px = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(px) and 0.0 < px < _MAX_SANE_EQUITY_PX


def reference_price(
    *, side: str, last: float | None, bid: float | None = None, ask: float | None = None
) -> float:
    """Price to lean through: bid for sells, ask for buys, else last."""
    sell = str(side).lower() == "sell"
    ordered = (bid, last, ask) if sell else (ask, last, bid)
    for raw in ordered:
        if is_sane_equity_price(raw):
            return float(raw)
    raise ValueError("asx_requires_reference_price")


def aggressive_limit_price(*, side: str, last: float, flatten: bool = False) -> float:
    if not is_sane_equity_price(last):
        raise ValueError("last must be a sane positive price")
    px = float(last)
    pct = _AU_FLATTEN_SLIP_PCT if flatten else _AU_SLIP_PCT
    slip = max(px * pct, _AU_MIN_SLIP)
    raw = px - slip if str(side).lower() == "sell" else px + slip
    out = round_equity_price(max(0.01, raw))
    if out is None or not is_sane_equity_price(out):
        raise ValueError("limit rounded to zero")
    return float(out)


def apply_marketable_limit(
    *,
    venue: str | None,
    exchange: str | None = None,
    side: str,
    order_type: str | None,
    limit_price: float | None,
    last: float | None,
    bid: float | None = None,
    ask: float | None = None,
    flatten: bool = False,
) -> tuple[str, float]:
    """Return (limit, price) for an AU/ASX market or limit order.

    Never leaves a native market order. Never turns a resting stop into a
    limit: callers must skip this function for those types, and a direct call
    fails closed instead of selling through the quote.
    """
    if is_resting_stop(order_type):
        raise ValueError("resting_stop_is_not_a_marketable_limit")
    if not uses_marketable_limit(venue, exchange):
        raise ValueError("not_a_marketable_limit_venue")
    ref = reference_price(side=side, last=last, bid=bid, ask=ask)
    want = aggressive_limit_price(side=side, last=ref, flatten=flatten)
    otype = str(order_type or "market").lower()
    existing = float(limit_price) if is_sane_equity_price(limit_price) else None
    if otype in {"limit", "lmt"} and existing is not None:
        if str(side).lower() == "sell" and existing <= want:
            return "limit", existing
        if str(side).lower() != "sell" and existing >= want:
            return "limit", existing
    return "limit", want
