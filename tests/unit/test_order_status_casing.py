"""The `orders` table holds two dialects for the same order.

`InternalOrderState` writes `SUBMITTED`/`CANCELLED`; broker adapters write
`submitted`/`canceled`; IBKR reports its own `MKT`/`STP` codes back and adopted
rows kept them. Every membership test that listed one spelling silently skipped
rows written by the other writer — the dashboard's open-order panel missed every
`SUBMITTED` row, and reconciliation missed the `*_PENDING` states entirely.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.brokers.models import InternalOrderState, canonical_order_type
from app.core.database import Base
from app.execution.order_manager import (
    STOP_ORDER_TYPES,
    WORKING_ORDER_STATUSES,
    fold_status,
)
from app.execution.reconciliation import _OPEN_LOCAL_STATUSES, _fields_from_remote
from app.models import Order


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


def _order(**overrides: object) -> Order:
    defaults: dict[str, object] = {
        "id": uuid4(),
        "idempotency_key": f"k-{uuid4()}",
        "symbol": "GOOGL",
        "side": "sell",
        "qty": 100.0,
        "order_type": "stop",
        "status": "submitted",
    }
    defaults.update(overrides)
    return Order(**defaults)


def test_every_working_status_is_folded() -> None:
    assert all(s == s.lower() for s in WORKING_ORDER_STATUSES)
    assert all(s == s.lower() for s in _OPEN_LOCAL_STATUSES)
    assert all(s == s.lower() for s in STOP_ORDER_TYPES)


def test_the_pending_states_are_not_left_out() -> None:
    """Hand-listing both cases missed these; deriving from the enum cannot."""
    for state in (
        InternalOrderState.SUBMITTED,
        InternalOrderState.ACCEPTED,
        InternalOrderState.PARTIALLY_FILLED,
        InternalOrderState.CANCEL_PENDING,
        InternalOrderState.REPLACE_PENDING,
    ):
        assert fold_status(state.value) in WORKING_ORDER_STATUSES


def test_a_terminal_status_is_not_working() -> None:
    for value in ("FILLED", "filled", "CANCELLED", "canceled", "REJECTED"):
        assert fold_status(value) not in WORKING_ORDER_STATUSES


@pytest.mark.asyncio
async def test_both_dialects_are_returned_by_one_query(session: AsyncSession) -> None:
    session.add_all(
        [
            _order(status="SUBMITTED"),
            _order(status="submitted"),
            _order(status="ACCEPTED"),
            _order(status="partially_filled"),
            _order(status="FILLED"),
            _order(status="canceled"),
        ]
    )
    await session.flush()

    rows = (
        (
            await session.execute(
                select(Order).where(func.lower(Order.status).in_(list(WORKING_ORDER_STATUSES)))
            )
        )
        .scalars()
        .all()
    )

    assert len(rows) == 4


def test_ibkr_codes_fold_onto_the_internal_vocabulary() -> None:
    assert canonical_order_type("MKT") == "market"
    assert canonical_order_type("STP") == "stop"
    assert canonical_order_type("STP LMT") == "stop_limit"
    assert canonical_order_type("LMT") == "limit"
    # Already-internal spellings survive unchanged.
    assert canonical_order_type("stop_limit") == "stop_limit"
    assert canonical_order_type(None) == "market"


def test_an_unlearned_code_stays_visible_instead_of_becoming_market() -> None:
    """Defaulting would relabel an unknown type as a plain market order."""
    assert canonical_order_type("MIDPRICE") == "midprice"


def test_an_adopted_stop_is_stored_as_a_stop() -> None:
    """Adoption wrote IBKR's `stp` straight through, so it matched no stop query."""

    class _Remote:
        broker_order_id = "1"
        status = "accepted"
        filled_qty = 0
        raw = {"symbol": "googl", "side": "sell", "qty": 100, "order_type": "STP"}

    fields = _fields_from_remote(_Remote())

    assert fields["order_type"] in STOP_ORDER_TYPES
    assert fields["order_type"] == "stop"
