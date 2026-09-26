"""A position that has to be closed must still be closed on a bad day.

Two leaks, both of which let a triggered exit produce an alert and no order:

* `protective_exit` tested `verdict == "EXIT_INTENT_REQUIRED"`. The daily-loss
  and drawdown checks run *after* the exit checks and are portfolio-wide, so
  the day they fire they relabelled every position EMERGENCY_ACTION_REQUIRED
  and cancelled every time stop, take-profit and giveback in the book.
* Every exit rule was gated on `qty > 0` with long-only comparisons, so the
  short book had no stop, no target and no giveback at all.

Measured before the fix: 16 of 64 day positions and 2 of 2 scalps ran past
`max_holding_minutes` without closing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.core.config import Settings
from app.core.database import Base
from app.intraday.monitor import PositionMonitor
from app.intraday.service import protective_exit_reason
from app.models import PositionLifecycle
from app.universe.book_strategy import lock_level, playbook_take_profit

NOW = datetime(2026, 9, 26, 15, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


def _settings(**kwargs: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "test",
        "broker_provider": "mock",
        "broker_environment": "paper",
        "enable_broker_orders": False,
        "enable_live_trading": False,
        "starting_cash": 100_000.0,
    }
    base.update(kwargs)
    return Settings(**base)  # type: ignore[arg-type]


def _lifecycle(**overrides: object) -> PositionLifecycle:
    defaults: dict[str, object] = {
        "id": uuid4(),
        "symbol": "GOOGL",
        "status": "OPEN",
        "quantity": 100.0,
        "average_entry_price": 100.0,
        "current_price": 100.0,
        "stop_price": 98.0,
        "protection_submitted": True,
        "opened_at": NOW - timedelta(minutes=600),
        "max_holding_minutes": 390,
        "exit_policy": {"horizon": "day"},
    }
    defaults.update(overrides)
    return PositionLifecycle(**defaults)


async def _evaluate(
    session: AsyncSession, lc: PositionLifecycle, *, price: float, **kwargs: float
) -> list[str]:
    session.add(lc)
    await session.flush()
    monitor = PositionMonitor(session, settings=_settings())
    result = await monitor.evaluate(lc, current_price=price, equity=100_000.0, **kwargs)
    return list(result.reasons or [])


@pytest.mark.asyncio
async def test_a_bad_day_does_not_cancel_the_time_stop(session: AsyncSession) -> None:
    """Emergency verdict used to swallow the max-holding exit entirely."""
    lc = _lifecycle()
    reasons = await _evaluate(session, lc, price=100.0, daily_pnl_pct=-99.0)

    assert "daily_loss_limit" in reasons
    assert "max_holding_time" in reasons
    # The verdict is relabelled, but the exit decision no longer reads it.
    assert protective_exit_reason(reasons, stop_triggered=False) == "max_holding_time"


@pytest.mark.asyncio
async def test_the_time_stop_still_fires_on_a_calm_day(session: AsyncSession) -> None:
    lc = _lifecycle()
    reasons = await _evaluate(session, lc, price=100.0)

    assert "max_holding_time" in reasons


@pytest.mark.asyncio
async def test_a_short_book_position_can_hit_its_stop(session: AsyncSession) -> None:
    """A short's stop sits above the mark; `price <= stop` never fired."""
    lc = _lifecycle(quantity=-100.0, stop_price=102.0, max_holding_minutes=None)
    reasons = await _evaluate(session, lc, price=103.0)

    assert "stop_triggered" in reasons


@pytest.mark.asyncio
async def test_a_short_that_has_not_reached_its_stop_is_left_alone(
    session: AsyncSession,
) -> None:
    lc = _lifecycle(quantity=-100.0, stop_price=102.0, max_holding_minutes=None)
    reasons = await _evaluate(session, lc, price=95.0)

    assert "stop_triggered" not in reasons


@pytest.mark.asyncio
async def test_a_short_close_to_its_stop_is_flagged(session: AsyncSession) -> None:
    """`(price - stop)` went negative for shorts, so proximity never showed."""
    lc = _lifecycle(quantity=-100.0, stop_price=102.0, max_holding_minutes=None)
    reasons = await _evaluate(session, lc, price=101.5)

    assert "stop_proximity" in reasons


@pytest.mark.asyncio
async def test_a_short_book_position_can_take_profit(session: AsyncSession) -> None:
    lc = _lifecycle(
        quantity=-100.0,
        stop_price=102.0,
        take_profit_price=97.0,
        max_holding_minutes=None,
    )
    reasons = await _evaluate(session, lc, price=96.5)

    assert "take_profit_triggered" in reasons


@pytest.mark.asyncio
async def test_a_short_above_its_target_does_not_take_profit(session: AsyncSession) -> None:
    lc = _lifecycle(
        quantity=-100.0,
        stop_price=102.0,
        take_profit_price=97.0,
        max_holding_minutes=None,
    )
    reasons = await _evaluate(session, lc, price=99.0)

    assert "take_profit_triggered" not in reasons


@pytest.mark.asyncio
async def test_a_long_still_takes_profit(session: AsyncSession) -> None:
    lc = _lifecycle(take_profit_price=103.0, max_holding_minutes=None)
    reasons = await _evaluate(session, lc, price=103.5)

    assert "take_profit_triggered" in reasons


@pytest.mark.asyncio
async def test_a_short_that_gave_back_a_real_gain_is_closed(session: AsyncSession) -> None:
    """Best print for a short is its lowest; the rule read `peak` and never armed."""
    lc = _lifecycle(
        quantity=-100.0,
        stop_price=102.0,
        take_profit_price=97.0,
        max_holding_minutes=None,
        # Ran to 98.0, past the 98.5 lock, and is now back above entry.
        exit_policy={"horizon": "day", "trough_price": 98.0},
    )
    reasons = await _evaluate(session, lc, price=100.5)

    assert "giveback_to_loss" in reasons


@pytest.mark.asyncio
async def test_a_short_that_never_ran_far_enough_is_not_closed(session: AsyncSession) -> None:
    lc = _lifecycle(
        quantity=-100.0,
        stop_price=102.0,
        take_profit_price=97.0,
        max_holding_minutes=None,
        exit_policy={"horizon": "day", "trough_price": 99.5},
    )
    reasons = await _evaluate(session, lc, price=100.5)

    assert "giveback_to_loss" not in reasons


def test_the_lock_level_mirrors_for_a_short() -> None:
    assert lock_level(entry=100.0, take_profit=103.0) == 101.5
    assert lock_level(entry=100.0, take_profit=97.0, long_side=False) == 98.5
    # A target on the wrong side of entry is not a target.
    assert lock_level(entry=100.0, take_profit=97.0) is None
    assert lock_level(entry=100.0, take_profit=103.0, long_side=False) is None


def test_the_playbook_target_mirrors_for_a_short() -> None:
    assert playbook_take_profit(entry=100.0, horizon="short") == 103.0
    assert playbook_take_profit(entry=100.0, horizon="short", long_side=False) == 97.0


def test_a_relabelled_verdict_cannot_suppress_the_exit() -> None:
    """The old test was `verdict == EXIT_INTENT_REQUIRED`; the verdict is gone."""
    assert protective_exit_reason(["daily_loss_limit"], stop_triggered=False) is None
    assert (
        protective_exit_reason(
            ["max_holding_time", "daily_loss_limit", "drawdown_limit"],
            stop_triggered=False,
        )
        == "max_holding_time"
    )


def test_a_hard_stop_outranks_every_other_reason() -> None:
    assert protective_exit_reason(["max_holding_time"], stop_triggered=True) == "hard_stop"
    assert protective_exit_reason(None, stop_triggered=True) == "hard_stop"


def test_a_review_only_reason_is_not_an_exit() -> None:
    """`max_holding_review` is the short/medium book's overnight check."""
    for reason in ("max_holding_review", "stop_proximity", "spread_wide", "position_concentration"):
        assert protective_exit_reason([reason], stop_triggered=False) is None


def test_the_most_urgent_reason_wins_over_append_order() -> None:
    assert (
        protective_exit_reason(
            ["giveback_to_loss", "max_holding_time", "stop_triggered"],
            stop_triggered=False,
        )
        == "stop_triggered"
    )
