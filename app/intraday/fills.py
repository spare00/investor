"""Load executions as FIFO fills. Fees stay unknown when the payload has none."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.brokers.venue_orders import is_sane_equity_price
from app.intraday.pnl import FillRecord
from app.models import Execution, Order

_FEE_KEYS = ("fee", "fees", "commission")


def fee_from_payload(raw: dict[str, Any] | None) -> float | None:
    """Return a recorded commission, or None when the fill did not carry one.

    A stored 0 is a known zero. A missing key is not.
    """
    payload = raw if isinstance(raw, dict) else {}
    for key in _FEE_KEYS:
        if key not in payload or payload[key] is None:
            continue
        try:
            value = float(payload[key])
        except (TypeError, ValueError):
            return None
        if value < 0 or value != value:
            return None
        return value
    report = payload.get("commissionReport")
    if isinstance(report, dict) and report.get("commission") is not None:
        return fee_from_payload({"commission": report.get("commission")})
    return None


def fill_from_execution(execution: Execution, order: Order | None) -> FillRecord | None:
    if order is None:
        return None
    side = str(order.side or "").strip().lower()
    if side not in {"buy", "sell"}:
        return None
    if execution.executed_at is None:
        return None
    try:
        qty = float(execution.qty)
        price = float(execution.price)
    except (TypeError, ValueError):
        return None
    if qty <= 1e-12 or not is_sane_equity_price(price):
        return None
    order_raw = order.raw_payload if isinstance(order.raw_payload, dict) else {}
    exec_raw = execution.raw_payload if isinstance(execution.raw_payload, dict) else {}
    venue = str(exec_raw.get("venue") or order_raw.get("venue") or "").strip().upper()
    currency = str(exec_raw.get("currency") or order_raw.get("currency") or "").strip().upper()
    if not currency and venue == "AU":
        currency = "AUD"
    elif not currency and venue == "US":
        currency = "USD"
    exchange = str(exec_raw.get("exchange") or order_raw.get("exchange") or "").strip().upper()
    account = str(
        exec_raw.get("account") or order_raw.get("account") or order_raw.get("account_id") or ""
    ).strip()
    return FillRecord(
        symbol=str(execution.symbol or "").upper(),
        side=side,
        quantity=qty,
        price=price,
        executed_at=execution.executed_at,
        venue=venue,
        currency=currency,
        exchange=exchange,
        account=account,
        fee=fee_from_payload(exec_raw),
        fill_id=str(execution.id),
    )


async def load_fill_records(
    session: AsyncSession,
    *,
    symbol: str | None = None,
) -> tuple[list[FillRecord], int]:
    stmt = select(Execution, Order).join(Order, Execution.order_id == Order.id)
    if symbol:
        stmt = stmt.where(Execution.symbol == symbol.upper())
    rows = (await session.execute(stmt)).all()
    fills: list[FillRecord] = []
    skipped = 0
    for execution, order in rows:
        fill = fill_from_execution(execution, order)
        if fill is None:
            skipped += 1
            continue
        fills.append(fill)
    return fills, skipped
