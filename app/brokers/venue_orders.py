"""Venue-specific order shaping.

ASX (and IBKR paper ASX) does not fill native MKT the way US SMART does.
Convert those to an aggressive limit so flatten/stop exits actually trade.
"""

from __future__ import annotations

import math

from app.brokers.pricing import round_equity_price

# Through-the-market offset. 0.8% was not enough once the snapshot was delayed
# or missing; IBKR still collars ~10%, so 2.5% stays inside the band.
_AU_SLIP_PCT = 0.025
_AU_MIN_SLIP = 0.02
# IBKR unset / NaN ticks often show up as DBL_MAX or 0.
_MAX_SANE_EQUITY_PX = 1_000_000.0


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


def reference_price(*, side: str, last: float | None, bid: float | None = None, ask: float | None = None) -> float:
    """Price to lean through: bid for sells, ask for buys, else last."""
    sell = str(side).lower() == "sell"
    ordered = (bid, last, ask) if sell else (ask, last, bid)
    for raw in ordered:
        if is_sane_equity_price(raw):
            return float(raw)
    raise ValueError("asx_requires_reference_price")


def aggressive_limit_price(*, side: str, last: float) -> float:
    if not is_sane_equity_price(last):
        raise ValueError("last must be a sane positive price")
    px = float(last)
    slip = max(px * _AU_SLIP_PCT, _AU_MIN_SLIP)
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
) -> tuple[str, float]:
    """Return (limit, price) for AU/ASX. Never leaves a native market order."""
    if not uses_marketable_limit(venue, exchange):
        raise ValueError("not_a_marketable_limit_venue")
    ref = reference_price(side=side, last=last, bid=bid, ask=ask)
    want = aggressive_limit_price(side=side, last=ref)
    otype = str(order_type or "market").lower()
    existing = float(limit_price) if is_sane_equity_price(limit_price) else None
    if otype in {"limit", "lmt"} and existing is not None:
        if str(side).lower() == "sell" and existing <= want:
            return "limit", existing
        if str(side).lower() != "sell" and existing >= want:
            return "limit", existing
    return "limit", want
