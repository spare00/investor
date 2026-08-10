"""Static FX helpers for dual-book risk sizing (no live FX feed).

Rates are quoted as ``CCY1CCY2`` meaning 1 unit of CCY1 equals N units of CCY2
(e.g. ``AUDUSD:0.65`` → 1 AUD = 0.65 USD). Inverse pairs are derived when missing.
"""

from __future__ import annotations

from typing import Mapping


def parse_fx_rates(raw: str | Mapping[str, float] | None) -> dict[str, float]:
    """Parse ``AUDUSD:0.65,EURUSD:1.08`` or a mapping into uppercase pair→rate."""
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        out: dict[str, float] = {}
        for key, val in raw.items():
            pair = str(key).upper().replace("/", "").replace("_", "").strip()
            try:
                rate = float(val)
            except (TypeError, ValueError):
                continue
            if len(pair) == 6 and rate > 0:
                out[pair] = rate
        return out
    out = {}
    text = str(raw).strip()
    if not text:
        return out
    for part in text.split(","):
        piece = part.strip()
        if not piece:
            continue
        if ":" in piece:
            pair_s, rate_s = piece.split(":", 1)
        elif "=" in piece:
            pair_s, rate_s = piece.split("=", 1)
        else:
            continue
        pair = pair_s.upper().replace("/", "").replace("_", "").strip()
        try:
            rate = float(rate_s.strip())
        except ValueError:
            continue
        if len(pair) == 6 and rate > 0:
            out[pair] = rate
    return out


def fx_rate(
    rates: Mapping[str, float] | None,
    from_ccy: str,
    to_ccy: str,
) -> float | None:
    """Return multiply factor: ``amount_to = amount_from * fx_rate(...)``."""
    a = (from_ccy or "").upper().strip()
    b = (to_ccy or "").upper().strip()
    if not a or not b:
        return None
    if a == b:
        return 1.0
    table = rates or {}
    direct = table.get(f"{a}{b}")
    if direct is not None and direct > 0:
        return float(direct)
    inverse = table.get(f"{b}{a}")
    if inverse is not None and inverse > 0:
        return 1.0 / float(inverse)
    return None


def convert_amount(
    amount: float,
    *,
    from_ccy: str,
    to_ccy: str,
    rates: Mapping[str, float] | None,
) -> float | None:
    rate = fx_rate(rates, from_ccy, to_ccy)
    if rate is None:
        return None
    return float(amount) * rate
