"""Execution FIFO keeps currencies apart and does not invent a cost basis."""

from __future__ import annotations

from datetime import UTC, datetime

from app.intraday.pnl import FillRecord, lifecycle_pnl, reconstruct_fifo
from app.performance.fifo_ledger import books_from_ledger


def _fill(
    symbol: str,
    side: str,
    qty: float,
    price: float,
    when: datetime,
    *,
    currency: str,
    venue: str,
    fee: float | None = None,
    fill_id: str = "",
) -> FillRecord:
    return FillRecord(
        symbol=symbol,
        side=side,
        quantity=qty,
        price=price,
        executed_at=when,
        venue=venue,
        currency=currency,
        fee=fee,
        fill_id=fill_id or f"{symbol}-{side}-{when.isoformat()}",
    )


def test_partial_round_trip_uses_fill_time_and_leaves_the_lot() -> None:
    opened = datetime(2026, 9, 30, 4, 32, 42, tzinfo=UTC)
    closed = datetime(2026, 9, 30, 4, 36, 2, tzinfo=UTC)
    ledger = reconstruct_fifo(
        [
            _fill("CBA", "buy", 326, 170.10, opened, currency="AUD", venue="AU", fill_id="b"),
            _fill("CBA", "sell", 100, 169.50, closed, currency="AUD", venue="AU", fill_id="s"),
        ]
    )
    book = ledger.books[0]
    assert book.currency == "AUD"
    assert len(book.closes) == 1
    close = book.closes[0]
    assert close.gross_pnl == round((169.50 - 170.10) * 100, 4)
    assert close.opened_at == opened
    assert close.closed_at == closed
    assert close.fee is None
    assert abs(book.open_lots[0].quantity - 226) < 1e-9


def test_sell_without_a_lot_is_unknown_not_zero() -> None:
    ledger = reconstruct_fifo(
        [
            _fill(
                "CBA",
                "sell",
                326,
                169.0,
                datetime(2026, 9, 30, 4, 36, tzinfo=UTC),
                currency="AUD",
                venue="AU",
            )
        ]
    )
    close = ledger.books[0].closes[0]
    assert close.gross_pnl is None
    assert close.basis == "unknown_opening_inventory"


def test_currencies_are_not_one_book() -> None:
    when = datetime(2026, 9, 30, 14, 0, tzinfo=UTC)
    later = datetime(2026, 9, 30, 15, 0, tzinfo=UTC)
    ledger = reconstruct_fifo(
        [
            _fill("BHP", "buy", 10, 40.0, when, currency="AUD", venue="AU", fill_id="ab"),
            _fill("BHP", "sell", 10, 39.0, later, currency="AUD", venue="AU", fill_id="as"),
            _fill("AAPL", "buy", 10, 100.0, when, currency="USD", venue="US", fill_id="ub"),
            _fill("AAPL", "sell", 10, 110.0, later, currency="USD", venue="US", fill_id="us"),
        ]
    )
    by_ccy = {book.currency: book.closes[0].gross_pnl for book in ledger.books}
    assert by_ccy["AUD"] == -10.0
    assert by_ccy["USD"] == 100.0


def test_known_fee_is_allocated_and_a_missing_fee_stays_missing() -> None:
    opened = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
    closed = datetime(2026, 9, 1, 15, 0, tzinfo=UTC)
    ledger = reconstruct_fifo(
        [
            _fill(
                "SPY", "buy", 10, 100.0, opened, currency="USD", venue="US", fee=1.0, fill_id="b"
            ),
            _fill(
                "SPY", "sell", 4, 110.0, closed, currency="USD", venue="US", fee=0.4, fill_id="s"
            ),
        ]
    )
    close = ledger.books[0].closes[0]
    assert close.gross_pnl == 40.0
    assert close.fee == 0.8

    missing = reconstruct_fifo(
        [
            _fill(
                "SPY", "buy", 10, 100.0, opened, currency="USD", venue="US", fee=1.0, fill_id="b"
            ),
            _fill("SPY", "sell", 4, 110.0, closed, currency="USD", venue="US", fill_id="s"),
        ]
    )
    assert missing.books[0].closes[0].fee is None


def test_performance_books_do_not_sum_currencies() -> None:
    when = datetime(2026, 9, 30, 14, 0, tzinfo=UTC)
    later = datetime(2026, 9, 30, 15, 0, tzinfo=UTC)
    ledger = reconstruct_fifo(
        [
            _fill("BHP", "buy", 10, 40.0, when, currency="AUD", venue="AU", fill_id="ab"),
            _fill("BHP", "sell", 10, 39.0, later, currency="AUD", venue="AU", fill_id="as"),
            _fill("AAPL", "buy", 10, 100.0, when, currency="USD", venue="US", fill_id="ub"),
            _fill("AAPL", "sell", 10, 110.0, later, currency="USD", venue="US", fill_id="us"),
        ]
    )
    report = books_from_ledger(
        ledger,
        lifecycles=[],
        watchlist_hz={},
        period_start=datetime(2026, 9, 1, tzinfo=UTC),
        period_end=datetime(2026, 10, 1, tzinfo=UTC),
    )
    assert report["trade_count"] == 0
    assert report["by_currency"]["AUD"]["trade_count"] == 1
    assert report["by_currency"]["USD"]["trade_count"] == 1
    assert report["by_currency"]["AUD"]["expectancy"].value == -10.0
    assert report["by_currency"]["USD"]["expectancy"].value == 100.0
    assert report["by_currency"]["AUD"]["fee_treatment"] == "gross_fees_unrecorded"
    assert "dividend" in report["unobserved_adjustments"]


def test_unmarked_zero_is_not_the_last_mark() -> None:
    class _Row:
        realized_pl = 0.0
        unrealized_pl = -9528.0
        metadata_json: dict[str, object] = {}

    assert lifecycle_pnl(_Row()) is None
