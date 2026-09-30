"""Deterministic FIFO P&L helpers.

Long inventory only. A sell with no open lot is an unknown opening position,
not a short and not a zero-cost round trip. Missing fees stay missing.
Dividends, splits, FX, and cash transfers are not in the execution stream.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
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
    unmatched_quantity: float = 0.0


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
    executed_at: datetime | None = None,
) -> PnLResult:
    """Buy adds lots; sell closes oldest lots (FIFO).

    Quantity that sells through an empty book is ``unmatched_quantity``. It is
    not given a zero cost basis.
    """
    remaining = [Lot(lot.quantity, lot.price, lot.opened_at) for lot in lots]
    realized = 0.0
    qty_left = quantity
    unmatched = 0.0
    opened = executed_at or datetime.now(UTC)
    if side.lower() == "buy":
        remaining.append(Lot(quantity, price, opened))
    else:
        while qty_left > 1e-12 and remaining:
            lot = remaining[0]
            take = min(lot.quantity, qty_left)
            realized += (price - lot.price) * take
            lot.quantity -= take
            qty_left -= take
            if lot.quantity <= 1e-12:
                remaining.pop(0)
        if qty_left > 1e-12:
            unmatched = qty_left
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
        unmatched_quantity=round(unmatched, 8),
    )


_UNOBSERVED_ADJUSTMENTS = ("dividend", "split", "fx", "cash_transfer")


@dataclass(frozen=True, slots=True)
class FillRecord:
    symbol: str
    side: str
    quantity: float
    price: float
    executed_at: datetime
    venue: str = ""
    currency: str = ""
    exchange: str = ""
    account: str = ""
    fee: float | None = None
    fill_id: str = ""


@dataclass(frozen=True, slots=True)
class FifoClose:
    symbol: str
    venue: str
    currency: str
    exchange: str
    account: str
    quantity: float
    entry_price: float | None
    exit_price: float | None
    opened_at: datetime | None
    closed_at: datetime | None
    gross_pnl: float | None
    fee: float | None
    basis: str


@dataclass(frozen=True, slots=True)
class OpenLot:
    quantity: float
    price: float
    opened_at: datetime
    fee_per_share: float | None


@dataclass(slots=True)
class FifoBook:
    account: str
    venue: str
    currency: str
    exchange: str
    symbol: str
    closes: list[FifoClose] = field(default_factory=list)
    open_lots: list[OpenLot] = field(default_factory=list)


@dataclass(slots=True)
class FifoLedger:
    books: list[FifoBook] = field(default_factory=list)
    skipped_fills: int = 0
    unobserved_adjustments: tuple[str, ...] = _UNOBSERVED_ADJUSTMENTS


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _blank(value: str, *, missing: frozenset[str]) -> bool:
    return value.strip().upper() in missing


def _adopt_single_identity(fills: list[FillRecord]) -> list[FillRecord]:
    """Blank account, venue, or currency joins the symbol's only known value.

    Two known currencies stay apart. An unknown currency is never folded into
    a guess, and a blank one is filled only when the symbol has a single currency.
    """
    by_symbol: dict[str, list[FillRecord]] = {}
    for fill in fills:
        by_symbol.setdefault(fill.symbol.upper(), []).append(fill)
    adopted: list[FillRecord] = []
    for symbol, items in by_symbol.items():
        accounts = {f.account.strip() for f in items if f.account.strip()}
        venues = {
            f.venue.strip().upper()
            for f in items
            if not _blank(f.venue, missing=frozenset({"", "UNKNOWN"}))
        }
        currencies = {
            f.currency.strip().upper()
            for f in items
            if not _blank(f.currency, missing=frozenset({"", "UNKNOWN"}))
        }
        account = next(iter(accounts)) if len(accounts) == 1 else ""
        venue = next(iter(venues)) if len(venues) == 1 else ""
        currency = next(iter(currencies)) if len(currencies) == 1 else ""
        for fill in items:
            next_account = fill.account.strip() or account
            next_venue = fill.venue.strip().upper()
            if _blank(next_venue, missing=frozenset({"", "UNKNOWN"})):
                next_venue = venue or "UNKNOWN"
            next_currency = fill.currency.strip().upper()
            if _blank(next_currency, missing=frozenset({"", "UNKNOWN"})):
                next_currency = currency or "UNKNOWN"
            adopted.append(
                replace(
                    fill,
                    symbol=symbol,
                    side=fill.side.strip().lower(),
                    account=next_account,
                    venue=next_venue,
                    currency=next_currency,
                    exchange=fill.exchange.strip().upper(),
                )
            )
    return adopted


def reconstruct_fifo(fills: list[FillRecord]) -> FifoLedger:
    """Match buys and sells per account, venue, currency, and symbol.

    Exchange splits the book only when the same identity traded on more than
    one exchange. A missing exchange does not become its own book when the
    others agree.
    """
    usable: list[FillRecord] = []
    skipped = 0
    for fill in fills:
        side = fill.side.strip().lower()
        when = fill.executed_at
        if (
            side not in {"buy", "sell"}
            or when is None
            or fill.quantity <= 1e-12
            or fill.price <= 0
            or fill.price != fill.price
        ):
            skipped += 1
            continue
        usable.append(fill)
    grouped: dict[tuple[str, str, str, str], list[FillRecord]] = {}
    for fill in _adopt_single_identity(usable):
        key = (fill.account, fill.venue, fill.currency, fill.symbol)
        grouped.setdefault(key, []).append(fill)

    books: list[FifoBook] = []
    for key, items in grouped.items():
        exchanges = {f.exchange for f in items if f.exchange}
        if len(exchanges) <= 1:
            books.append(_fifo_book(key, next(iter(exchanges), ""), items))
            continue
        buckets: dict[str, list[FillRecord]] = {}
        for fill in items:
            buckets.setdefault(fill.exchange or "UNKNOWN", []).append(fill)
        for exchange, subset in buckets.items():
            books.append(_fifo_book(key, exchange, subset))
    books.sort(key=lambda book: (book.currency, book.venue, book.symbol, book.account))
    return FifoLedger(books=books, skipped_fills=skipped)


def _fifo_book(key: tuple[str, str, str, str], exchange: str, items: list[FillRecord]) -> FifoBook:
    account, venue, currency, symbol = key
    ordered = sorted(
        items,
        key=lambda fill: (as_utc(fill.executed_at), 0 if fill.side == "buy" else 1, fill.fill_id),
    )
    lots: list[OpenLot] = []
    closes: list[FifoClose] = []
    for fill in ordered:
        when = as_utc(fill.executed_at)
        if fill.side == "buy":
            fee_per = (
                None
                if fill.fee is None or fill.quantity <= 0
                else float(fill.fee) / float(fill.quantity)
            )
            lots.append(
                OpenLot(
                    quantity=float(fill.quantity),
                    price=float(fill.price),
                    opened_at=when,
                    fee_per_share=fee_per,
                )
            )
            continue
        qty_left = float(fill.quantity)
        while qty_left > 1e-12 and lots:
            lot = lots[0]
            take = min(lot.quantity, qty_left)
            gross = (float(fill.price) - lot.price) * take
            buy_fee = None if lot.fee_per_share is None else lot.fee_per_share * take
            sell_fee = None
            if fill.fee is not None and fill.quantity > 0:
                sell_fee = float(fill.fee) * (take / float(fill.quantity))
            fee = None if buy_fee is None or sell_fee is None else buy_fee + sell_fee
            closes.append(
                FifoClose(
                    symbol=symbol,
                    venue=venue,
                    currency=currency,
                    exchange=exchange,
                    account=account,
                    quantity=take,
                    entry_price=lot.price,
                    exit_price=float(fill.price),
                    opened_at=lot.opened_at,
                    closed_at=when,
                    gross_pnl=round(gross, 4),
                    fee=None if fee is None else round(fee, 4),
                    basis="fill",
                )
            )
            left = lot.quantity - take
            qty_left -= take
            if left <= 1e-12:
                lots.pop(0)
            else:
                lots[0] = replace(lot, quantity=left)
        if qty_left > 1e-12:
            closes.append(
                FifoClose(
                    symbol=symbol,
                    venue=venue,
                    currency=currency,
                    exchange=exchange,
                    account=account,
                    quantity=qty_left,
                    entry_price=None,
                    exit_price=float(fill.price),
                    opened_at=None,
                    closed_at=when,
                    gross_pnl=None,
                    fee=None,
                    basis="unknown_opening_inventory",
                )
            )
    return FifoBook(
        account=account,
        venue=venue,
        currency=currency,
        exchange=exchange,
        symbol=symbol,
        closes=closes,
        open_lots=lots,
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


def lifecycle_pnl(lc: Any) -> float | None:
    """Fill-based closed P&L, or None when the row does not have one.

    A stored zero without a fill basis is unknown, not a scratch and not the
    last mark. A mark that was copied into realized_pl is not a fill either.
    Legacy non-zero values with no basis flag are still returned: they cannot
    be told apart from an older fill stamp.
    """
    meta = dict(getattr(lc, "metadata_json", None) or {})
    if meta.get("pnl_unavailable"):
        return None
    basis = str(meta.get("pnl_basis") or "")
    if basis == "mark":
        return None
    realized = getattr(lc, "realized_pl", None)
    if realized is None:
        return None
    value = float(realized)
    if abs(value) <= 1e-9 and basis not in {"fill", "fill_gross"}:
        return None
    return value


def stamp_lifecycle_close_pnl(
    lc: Any,
    *,
    realized: float | None = None,
    filled_at: datetime | None = None,
    basis: str | None = None,
) -> float | None:
    """Record a close from a fill, or mark the P&L unknown.

    The last quote is not a fill. Calling this without ``realized`` leaves
    ``realized_pl`` at 0 so the non-null column stays populated, and sets
    ``pnl_unavailable`` so readers do not treat that 0 as a measured scratch.
    """
    meta = dict(getattr(lc, "metadata_json", None) or {})
    if realized is not None and basis in {"fill", "fill_gross"}:
        meta.pop("pnl_unavailable", None)
        meta["pnl_basis"] = basis
        if basis == "fill_gross":
            meta["fees_unrecorded"] = True
        lc.metadata_json = meta
        lc.realized_pl = round(float(realized), 4)
        if filled_at is not None:
            lc.closed_at = filled_at
            meta = dict(lc.metadata_json or {})
            meta["closed_at_source"] = "fill"
            lc.metadata_json = meta
        return float(lc.realized_pl)
    meta["pnl_unavailable"] = "no_fill"
    meta.pop("pnl_basis", None)
    lc.metadata_json = meta
    lc.realized_pl = 0.0
    return None
