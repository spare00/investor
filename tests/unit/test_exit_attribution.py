"""A reactive close still has to say why the position is gone.

Regression: every close in the book arrived through broker_position_sync,
which stamps only ``closed_by``. That matches no text pattern, so every trade
was filed as ``unknown`` and the expectancy report had nothing to group by.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.core.database import Base
from app.models import PositionLifecycle
from app.universe.entry_attribution import (
    ATTR_MISSING_KEY,
    EXIT_INFERRED_KEY,
    EXIT_MAX_HOLDING,
    EXIT_STOP,
    EXIT_TAKE_PROFIT,
    EXIT_UNKNOWN,
    copy_entry_attribution_to_lifecycle,
    infer_exit_reason,
    stamp_lifecycle_exit_reason,
)


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


NOW = datetime(2026, 9, 26, 20, 0, tzinfo=UTC)


def _closed(**overrides: object) -> PositionLifecycle:
    defaults: dict[str, object] = {
        "id": uuid4(),
        "symbol": "GOOGL",
        "status": "CLOSED",
        "quantity": 0.0,
        "average_entry_price": 100.0,
        "stop_price": 98.5,
        "take_profit_price": 103.0,
        "opened_at": NOW - timedelta(minutes=30),
        "closed_at": NOW,
        "max_holding_minutes": 390,
    }
    defaults.update(overrides)
    return PositionLifecycle(**defaults)


def test_a_mark_through_the_stop_is_a_stop() -> None:
    assert infer_exit_reason(_closed(current_price=98.2)) == EXIT_STOP


def test_a_mark_through_the_target_is_a_take_profit() -> None:
    assert infer_exit_reason(_closed(current_price=103.4)) == EXIT_TAKE_PROFIT


def _short(**overrides: object) -> PositionLifecycle:
    # Stop above entry is what marks the position as short.
    return _closed(average_entry_price=60.0, stop_price=61.5, take_profit_price=58.0, **overrides)


def test_short_side_levels_are_read_the_other_way_round() -> None:
    assert infer_exit_reason(_short(current_price=61.9)) == EXIT_STOP
    assert infer_exit_reason(_short(current_price=57.5)) == EXIT_TAKE_PROFIT
    # A rise on a short is a loss, not a target hit.
    assert infer_exit_reason(_short(current_price=60.4)) is None


def test_a_full_hold_that_hit_neither_level_is_a_max_holding_exit() -> None:
    lc = _closed(
        current_price=100.5,
        opened_at=NOW - timedelta(minutes=400),
        max_holding_minutes=390,
    )
    assert infer_exit_reason(lc) == EXIT_MAX_HOLDING


def test_a_close_between_the_levels_stays_honest() -> None:
    # Neither level reached, well inside the hold cap: nothing to claim.
    assert infer_exit_reason(_closed(current_price=100.4)) is None


def test_no_mark_means_no_inference() -> None:
    assert infer_exit_reason(_closed(current_price=None)) is None


def test_broker_sync_close_is_classified_instead_of_filed_as_unknown() -> None:
    lc = _closed(current_price=98.1, metadata_json={"closed_by": "broker_position_sync"})
    assert stamp_lifecycle_exit_reason(lc, raw="broker_position_sync") == EXIT_STOP
    assert lc.metadata_json[EXIT_INFERRED_KEY] is True


def test_an_observed_reason_is_never_overwritten_by_inference() -> None:
    lc = _closed(current_price=98.1, metadata_json={})
    assert stamp_lifecycle_exit_reason(lc, raw="giveback_exit") == "giveback_exit"
    assert EXIT_INFERRED_KEY not in lc.metadata_json


def test_unknown_survives_when_the_levels_say_nothing() -> None:
    lc = _closed(current_price=100.4, metadata_json={})
    assert stamp_lifecycle_exit_reason(lc, raw="broker_position_sync") == EXIT_UNKNOWN
    assert EXIT_INFERRED_KEY not in lc.metadata_json


@pytest.mark.asyncio
async def test_partial_tags_do_not_lock_out_the_rest(session: AsyncSession) -> None:
    """Regression: one present key short-circuited the whole lookup.

    A row stamped with only ``entry_source`` could never acquire the timing and
    trend the expectancy report actually groups by.
    """
    lc = _closed(quantity=10.0, status="OPEN", metadata_json={"entry_source": "cio"})
    session.add(lc)
    await session.flush()

    await copy_entry_attribution_to_lifecycle(session, lc)

    # Nothing to pull from here, but the row must stay open to a later intent.
    assert ATTR_MISSING_KEY not in lc.metadata_json
    assert lc.metadata_json["opened_quantity"] == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_a_closed_row_with_no_source_stops_being_re_queried(
    session: AsyncSession,
) -> None:
    lc = _closed(metadata_json={})
    session.add(lc)
    await session.flush()

    await copy_entry_attribution_to_lifecycle(session, lc)

    assert lc.metadata_json[ATTR_MISSING_KEY] is True


@pytest.mark.parametrize("cap", [None, 0])
def test_no_hold_cap_means_no_max_holding_claim(cap: int | None) -> None:
    lc = _closed(
        current_price=100.5,
        opened_at=NOW - timedelta(days=9),
        max_holding_minutes=cap,
    )
    assert infer_exit_reason(lc) is None
