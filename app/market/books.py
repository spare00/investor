"""Helpers for dual-venue portfolio book summaries (no FX conversion)."""

from __future__ import annotations

from typing import Any

from app.market.venues import Venue, venue_for_symbol
from app.core.config import Settings, get_settings


def summarize_venue_books(
    positions: list[dict[str, Any]],
    *,
    settings: Settings | None = None,
    equity: float | None = None,
) -> dict[str, dict[str, float]]:
    """Aggregate absolute market value / count / weight by venue.

    Market values are left in their native reporting units from the broker —
    do not sum US+AU notionals into one currency without an FX rate.
    """
    cfg = settings or get_settings()
    books: dict[str, dict[str, float]] = {
        Venue.US.value: {"market_value": 0.0, "positions": 0.0, "weight_pct": 0.0},
        Venue.AU.value: {"market_value": 0.0, "positions": 0.0, "weight_pct": 0.0},
    }
    for raw in positions:
        symbol = str(raw.get("symbol") or "").upper()
        qty = float(raw.get("quantity") if raw.get("quantity") is not None else raw.get("qty") or 0)
        if not symbol or abs(qty) < 1e-12:
            continue
        mv = abs(float(raw.get("market_value") or 0))
        venue = str(
            raw.get("venue")
            or venue_for_symbol(
                symbol,
                cfg,
                exchange=str(raw.get("exchange") or "") or None,
                currency=str(raw.get("currency") or "") or None,
            ).value
        )
        bucket = books.setdefault(
            venue, {"market_value": 0.0, "positions": 0.0, "weight_pct": 0.0}
        )
        bucket["market_value"] += mv
        bucket["positions"] += 1.0
    if equity and equity > 0:
        for bucket in books.values():
            # Weight only meaningful within base-currency equity; still useful as ops signal.
            bucket["weight_pct"] = bucket["market_value"] / equity * 100.0
    return books
