"""Broker adapter interface — no live orders in Phase 1."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(StrEnum):
    NEW = "new"
    ACCEPTED = "accepted"
    FILLED = "filled"
    PARTIAL = "partially_filled"
    CANCELED = "canceled"
    REJECTED = "rejected"


@dataclass(slots=True)
class OrderRequest:
    symbol: str
    side: OrderSide
    qty: float
    order_type: str
    limit_price: float | None = None
    stop_price: float | None = None
    idempotency_key: str | None = None
    time_in_force: str = "day"
    venue: str | None = None  # US | AU — IBKR contract routing hint
    con_id: int | None = None  # IBKR Contract ID when known (preferred over symbol qualify)


@dataclass(slots=True)
class OrderResult:
    broker_order_id: str
    status: OrderStatus
    submitted_at: datetime
    filled_qty: float = 0.0
    avg_fill_price: float | None = None
    raw: dict[str, object] | None = None


class BrokerClient(Protocol):
    """Execution boundary. LLM agents must never call this directly."""

    async def submit_order(self, request: OrderRequest) -> OrderResult: ...

    async def cancel_order(self, broker_order_id: str) -> OrderResult: ...

    async def get_positions(self) -> list[dict[str, object]]: ...

    async def get_account(self) -> dict[str, object]: ...
