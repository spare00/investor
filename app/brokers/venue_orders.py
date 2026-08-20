"""Venue-specific order shaping.

ASX (and IBKR paper ASX) does not fill native MKT the way US SMART does.
Convert those to an aggressive limit so flatten/stop exits actually trade.
"""

from __future__ import annotations

from app.brokers.pricing import round_equity_price

# Through-the-market offset so a flatten is marketable without becoming a stub.
_AU_SLIP_PCT = 0.008
_AU_MIN_SLIP = 0.02


def uses_marketable_limit(venue: str | None, exchange: str | None = None) -> bool:
    if str(venue or "").strip().upper() == "AU":
        return True
    return str(exchange or "").strip().upper() in {"ASX", "ASX2"}


def aggressive_limit_price(*, side: str, last: float) -> float:
    px = float(last)
    if px <= 0:
        raise ValueError("last must be positive")
    slip = max(px * _AU_SLIP_PCT, _AU_MIN_SLIP)
    raw = px - slip if str(side).lower() == "sell" else px + slip
    out = round_equity_price(max(0.01, raw))
    if out is None or out <= 0:
        raise ValueError("limit rounded to zero")
    return float(out)
