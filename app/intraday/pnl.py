"""Deterministic FIFO P&L helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


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
    remaining = [Lot(l.quantity, l.price, l.opened_at) for l in lots]
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
    unrealized = sum((mark - l.price) * l.quantity for l in remaining)
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
