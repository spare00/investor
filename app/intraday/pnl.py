"""Deterministic FIFO P&L helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(slots=True)
class Lot:
    quantity: float
    price: float
    opened_at: datetime


@dataclass(slots=True)
class PnLResult:
    gross_realized_pl: float
    net_realized_pl: float
    unrealized_pl: float
    fees: float
    estimated_slippage: float
    return_pct: float
    risk_adjusted_return: float | None
    method: str = "FIFO"
    remaining_lots: list[Lot] = field(default_factory=list)
    conflict_with_broker: bool = False


def apply_fill_fifo(
    lots: list[Lot],
    *,
    side: str,
    quantity: float,
    price: float,
    fee: float = 0.0,
    slippage: float = 0.0,
    mark_price: float | None = None,
    equity: float = 25_000.0,
    broker_realized: float | None = None,
) -> PnLResult:
    """Buy adds lots; sell closes oldest lots (FIFO)."""
    remaining = [Lot(lot.quantity, lot.price, lot.opened_at) for lot in lots]
    realized = 0.0
    qty_left = quantity
    if side.lower() == "buy":
        remaining.append(Lot(quantity, price, datetime.now(UTC)))
    else:
        while qty_left > 1e-12 and remaining:
            lot = remaining[0]
            take = min(lot.quantity, qty_left)
            realized += (price - lot.price) * take
            lot.quantity -= take
            qty_left -= take
            if lot.quantity <= 1e-12:
                remaining.pop(0)
    mark = mark_price if mark_price is not None else price
    unrealized = sum((mark - lot.price) * lot.quantity for lot in remaining)
    net = realized - fee - slippage
    ret = (net / equity * 100.0) if equity else 0.0
    conflict = False
    if broker_realized is not None and abs(broker_realized - realized) > 0.01:
        conflict = True
    return PnLResult(
        gross_realized_pl=round(realized, 4),
        net_realized_pl=round(net, 4),
        unrealized_pl=round(unrealized, 4),
        fees=fee,
        estimated_slippage=slippage,
        return_pct=round(ret, 6),
        risk_adjusted_return=None,
        remaining_lots=remaining,
        conflict_with_broker=conflict,
    )


def usable_mark(*candidates: float | None) -> float | None:
    """First candidate that could be a real equity print, else None.

    IBKR unset ticks arrive as 0 or DBL_MAX, and a mark derived from a short
    book's signed market value arrives sign-flipped. Any of those multiplied by
    quantity produces a fabricated P&L, so reject them before they are stored.
    """
    from app.brokers.venue_orders import is_sane_equity_price

    for value in candidates:
        if value is None:
            continue
        try:
            px = float(value)
        except (TypeError, ValueError):
            continue
        if is_sane_equity_price(px):
            return px
    return None


def lifecycle_pnl(lc: Any) -> float:
    """Closed-trade P&L. Prefer stamped realized; last mark if that was never written."""
    realized = float(getattr(lc, "realized_pl", None) or 0.0)
    if abs(realized) > 1e-9:
        return realized
    return float(getattr(lc, "unrealized_pl", None) or 0.0)


def stamp_lifecycle_close_pnl(lc: Any) -> float:
    """Write realized_pl before quantity is zeroed on broker-flat close.

    Falling back to unrealized_pl is only safe when the mark that produced it
    was itself a real price — otherwise the fallback launders a bad tick into a
    permanent realized number.
    """
    existing = float(getattr(lc, "realized_pl", None) or 0.0)
    if abs(existing) > 1e-9:
        return existing
    qty = float(getattr(lc, "quantity", None) or 0.0)
    entry = usable_mark(getattr(lc, "average_entry_price", None))
    last = usable_mark(getattr(lc, "current_price", None))
    if qty and entry is not None and last is not None:
        pnl = (last - entry) * qty
    elif last is not None:
        pnl = float(getattr(lc, "unrealized_pl", None) or 0.0)
    else:
        # No trustworthy mark ever landed: record flat rather than invent a move.
        pnl = 0.0
        meta = dict(getattr(lc, "metadata_json", None) or {})
        meta["pnl_unavailable"] = "no_usable_mark"
        lc.metadata_json = meta
    lc.realized_pl = round(float(pnl), 4)
    return float(lc.realized_pl)
