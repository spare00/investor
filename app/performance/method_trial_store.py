"""Open a method trial at entry and fill its exit once the facts are known."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.intraday.fills import load_fill_records
from app.intraday.pnl import as_utc, reconstruct_fifo
from app.models import MethodTrial, Order, PositionLifecycle, PositionSnapshotRecord
from app.performance.method_trial import holding_minutes, merge_exit, price_path
from app.performance.trade_lesson import (
    STRATEGY_VERSION,
    CloseFacts,
    intended_and_broker,
    judge_close,
    select_exit_order,
)


async def open_method_trial(
    session: AsyncSession,
    lifecycle: PositionLifecycle,
    *,
    captured_at_open: bool,
) -> MethodTrial | None:
    """Insert the entry row once. A second call leaves the first row alone."""
    existing = (
        await session.execute(
            select(MethodTrial).where(MethodTrial.position_lifecycle_id == lifecycle.id).limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    trial = dict(lifecycle.metadata_json or {}).get("trial")
    if not isinstance(trial, dict) or trial.get("frozen") is not True:
        return None
    opened = lifecycle.opened_at
    hold = trial.get("intended_hold_minutes")
    if hold is None:
        hold = lifecycle.max_holding_minutes
    row = MethodTrial(
        position_lifecycle_id=lifecycle.id,
        symbol=str(lifecycle.symbol or "").upper(),
        venue=_text(trial.get("venue") or lifecycle.venue),
        currency=_text(trial.get("currency") or lifecycle.currency),
        strategy_id=str(trial.get("strategy_id") or ""),
        strategy_version=str(trial.get("strategy_version") or STRATEGY_VERSION),
        horizon=_text(trial.get("horizon")),
        entry_reason=_text(trial.get("entry_reason")),
        entry_source=_text(trial.get("entry_source")),
        trend_at_entry=_text(trial.get("trend_at_entry")),
        score=_num(trial.get("score")),
        score_kind=_text(trial.get("score_kind")),
        stop_price=_num(trial.get("stop_price")),
        target_price=_num(trial.get("target_price")),
        entry_price=_num(
            trial.get("entry_price")
            if trial.get("entry_price") is not None
            else lifecycle.average_entry_price
        ),
        intended_hold_minutes=_int(hold),
        opened_at=opened,
        entry_captured_at_open=bool(captured_at_open),
        status="open",
        fees_known=False,
        counts_for_method=False,
    )
    if not row.strategy_id:
        return None
    session.add(row)
    await session.flush()
    return row


async def complete_method_trial(
    session: AsyncSession, lifecycle: PositionLifecycle
) -> MethodTrial | None:
    """Write exit facts. Known gross, verdict, and path are left in place."""
    row = (
        await session.execute(
            select(MethodTrial).where(MethodTrial.position_lifecycle_id == lifecycle.id).limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    observed = await _observe_exit(session, lifecycle, row)
    merged = merge_exit(_exit_state(row), observed)
    _write_exit(row, merged)
    await session.flush()
    return row


async def _observe_exit(
    session: AsyncSession, lifecycle: PositionLifecycle, row: MethodTrial
) -> dict[str, Any]:
    symbol = str(lifecycle.symbol or "").upper()
    orders = list(
        (await session.execute(select(Order).where(Order.symbol == symbol))).scalars().all()
    )
    sells = [
        {
            "idempotency_key": order.idempotency_key,
            "order_type": order.order_type,
            "status": order.status,
        }
        for order in orders
        if str(order.side or "").lower() == "sell"
    ]
    key, order_type = select_exit_order(sells)
    if key or order_type:
        intended, broker = intended_and_broker(key, order_type)
    else:
        intended, broker = None, None

    fills, _skipped = await load_fill_records(session, symbol=symbol)
    venue = str(row.venue or lifecycle.venue or "").upper()
    if venue and any(fill.venue == venue for fill in fills):
        fills = [fill for fill in fills if fill.venue == venue]
    currency = str(row.currency or lifecycle.currency or "").upper()
    ledger = reconstruct_fifo(fills)
    books = ledger.books
    if currency and any(book.currency == currency for book in books):
        books = [book for book in books if book.currency == currency]
    opened = row.opened_at or lifecycle.opened_at
    opened_at = as_utc(opened) if isinstance(opened, datetime) else None
    closes = []
    for book in books:
        for close in book.closes:
            if close.closed_at is None:
                continue
            if opened_at is not None and as_utc(close.closed_at) < opened_at:
                continue
            closes.append(close)
    known = [close for close in closes if close.gross_pnl is not None]
    unknown = [close for close in closes if close.gross_pnl is None]
    gross = None
    fee = None
    fees_known = False
    exit_price = None
    closed_at = lifecycle.closed_at
    exit_marks: list[tuple[datetime, float]] = []
    if known and not unknown:
        gross = round(sum(float(close.gross_pnl or 0.0) for close in known), 4)
        fee_parts = [close.fee for close in known]
        fees_known = all(part is not None for part in fee_parts)
        if fees_known:
            fee = round(sum(float(part or 0.0) for part in fee_parts), 4)
        last = max(known, key=lambda close: as_utc(close.closed_at))
        exit_price = _num(last.exit_price)
        closed_at = last.closed_at
        for close in known:
            px = _num(close.exit_price)
            if px is not None and close.closed_at is not None:
                exit_marks.append((as_utc(close.closed_at), px))

    snapshots = list(
        (
            await session.execute(
                select(PositionSnapshotRecord)
                .where(PositionSnapshotRecord.position_lifecycle_id == lifecycle.id)
                .order_by(PositionSnapshotRecord.as_of)
            )
        )
        .scalars()
        .all()
    )
    marks: list[tuple[datetime, float]] = []
    for snap in snapshots:
        px = _num(snap.current_price)
        if px is not None and snap.as_of is not None:
            marks.append((as_utc(snap.as_of), px))
    marks.extend(exit_marks)
    path = price_path(marks, stop=_num(row.stop_price), target=_num(row.target_price))
    lesson = judge_close(
        CloseFacts(
            symbol=symbol,
            horizon=row.horizon,
            entry_reason=row.entry_reason,
            strategy_id=row.strategy_id,
            intended_order_type=intended,
            broker_order_type=broker,
            gross_pnl=gross,
        )
    )
    return {
        "gross_pnl": gross,
        "fee": fee,
        "fees_known": fees_known,
        "exit_price": exit_price,
        "closed_at": closed_at,
        "holding_minutes": holding_minutes(
            as_utc(opened) if isinstance(opened, datetime) else None,
            as_utc(closed_at) if isinstance(closed_at, datetime) else None,
        ),
        "path": path,
        "execution_verdict": lesson.execution_verdict,
        "strategy_verdict": lesson.strategy_verdict,
        "cause": lesson.cause,
        "intended_order_type": intended,
        "broker_order_type": broker,
    }


def _exit_state(row: MethodTrial) -> dict[str, Any]:
    return {
        "status": row.status,
        "entry_captured_at_open": row.entry_captured_at_open,
        "gross_pnl": row.gross_pnl,
        "fee": row.fee,
        "fees_known": row.fees_known,
        "closed_at": row.closed_at,
        "holding_minutes": row.holding_minutes,
        "exit_price": row.exit_price,
        "path": row.path,
        "execution_verdict": row.execution_verdict,
        "strategy_verdict": row.strategy_verdict,
        "cause": row.cause,
        "intended_order_type": row.intended_order_type,
        "broker_order_type": row.broker_order_type,
        "counts_for_method": row.counts_for_method,
    }


def _write_exit(row: MethodTrial, merged: dict[str, Any]) -> None:
    row.status = str(merged.get("status") or "closed")
    row.gross_pnl = merged.get("gross_pnl")
    row.fee = merged.get("fee")
    row.fees_known = bool(merged.get("fees_known"))
    row.closed_at = merged.get("closed_at")
    row.holding_minutes = merged.get("holding_minutes")
    row.exit_price = merged.get("exit_price")
    row.path = merged.get("path")
    row.execution_verdict = merged.get("execution_verdict")
    row.strategy_verdict = merged.get("strategy_verdict")
    row.cause = merged.get("cause")
    row.intended_order_type = merged.get("intended_order_type")
    row.broker_order_type = merged.get("broker_order_type")
    row.counts_for_method = bool(merged.get("counts_for_method"))


def trial_view(row: MethodTrial) -> dict[str, Any]:
    return {
        "strategy_id": row.strategy_id,
        "symbol": row.symbol,
        "currency": row.currency,
        "status": row.status,
        "entry_captured_at_open": row.entry_captured_at_open,
        "execution_verdict": row.execution_verdict,
        "cause": row.cause,
        "gross_pnl": row.gross_pnl,
        "counts_for_method": row.counts_for_method,
        "path": row.path,
        "fees_known": row.fees_known,
        "holding_minutes": row.holding_minutes,
    }


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
