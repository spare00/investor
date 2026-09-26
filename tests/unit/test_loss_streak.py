"""The consecutive-loss circuit breaker must see a real streak.

Regression: nothing ever computed ``consecutive_losses``, so the schema default
of 0 reached the risk engine on every call and both loss gates — cooldown at 3,
halt-day at 5 — were unreachable while the book ran a 48-trade losing streak.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.core.config import clear_settings_cache, get_settings
from app.core.database import Base
from app.execution.position_manager import PositionManager
from app.models import PositionLifecycle
from app.risk import loss_streak

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


@pytest.fixture(autouse=True)
def _reset() -> None:
    clear_settings_cache()
    yield
    clear_settings_cache()


def _closes(*pnls: float | None) -> list[tuple[datetime, float | None]]:
    """Newest-first closes, one minute apart."""
    return [(NOW - timedelta(minutes=i), pnl) for i, pnl in enumerate(pnls)]


def test_counts_back_to_the_first_win() -> None:
    result = loss_streak(_closes(-10.0, -5.0, -1.0, 40.0, -99.0), cooldown_minutes=30)
    assert result.consecutive_losses == 3


def test_a_win_on_top_clears_the_streak() -> None:
    result = loss_streak(_closes(2.0, -5.0, -5.0, -5.0), cooldown_minutes=30)
    assert result.consecutive_losses == 0
    assert result.cooldown_until is None


def test_unknown_pnl_neither_counts_nor_resets() -> None:
    # A repaired row (P&L zeroed as unrecoverable) must not reopen the gate.
    result = loss_streak(_closes(-10.0, None, -10.0, 5.0), cooldown_minutes=30)
    assert result.consecutive_losses == 2


def test_flat_close_is_neutral() -> None:
    result = loss_streak(_closes(-10.0, 0.0, -10.0), cooldown_minutes=30)
    assert result.consecutive_losses == 2


def test_cooldown_runs_from_the_most_recent_loss() -> None:
    result = loss_streak(_closes(-10.0, -10.0), cooldown_minutes=30)
    assert result.last_loss_at == NOW
    assert result.cooldown_until == NOW + timedelta(minutes=30)


def test_no_closes_is_a_clean_slate() -> None:
    result = loss_streak([], cooldown_minutes=30)
    assert result == loss_streak(_closes(7.0), cooldown_minutes=30)
    assert result.consecutive_losses == 0


def _closed(pnl: float | None, *, minutes_ago: int, when: datetime = NOW) -> PositionLifecycle:
    return PositionLifecycle(
        id=uuid4(),
        symbol=f"T{minutes_ago}",
        status="CLOSED",
        quantity=0.0,
        average_entry_price=10.0,
        realized_pl=pnl,
        closed_at=when - timedelta(minutes=minutes_ago),
    )


@pytest.mark.asyncio
async def test_manager_reads_the_streak_out_of_the_book(session: AsyncSession) -> None:
    for i, pnl in enumerate([-100.0, -50.0, -25.0, 10.0]):
        session.add(_closed(pnl, minutes_ago=i))
    await session.flush()

    mgr = PositionManager(session, settings=get_settings())
    streak = await mgr.current_loss_streak(since=NOW - timedelta(hours=1))

    assert streak.consecutive_losses == 3
    assert streak.cooldown_until is not None


@pytest.mark.asyncio
async def test_open_positions_are_not_part_of_the_streak(session: AsyncSession) -> None:
    session.add(_closed(-100.0, minutes_ago=1))
    losing_but_open = _closed(-500.0, minutes_ago=0)
    losing_but_open.status = "OPEN"
    session.add(losing_but_open)
    await session.flush()

    mgr = PositionManager(session, settings=get_settings())
    streak = await mgr.current_loss_streak(since=NOW - timedelta(hours=1))

    assert streak.consecutive_losses == 1


@pytest.mark.asyncio
async def test_yesterdays_losses_do_not_halt_today(session: AsyncSession) -> None:
    # Halt-day is a daily gate; an all-time streak would deadlock the book
    # forever, because past the threshold no trade can open to break it.
    for i in range(6):
        session.add(_closed(-100.0, minutes_ago=i, when=NOW - timedelta(days=1)))
    await session.flush()

    mgr = PositionManager(session, settings=get_settings())
    today = await mgr.current_loss_streak(since=NOW.replace(hour=0, minute=0))

    assert today.consecutive_losses == 0


@pytest.mark.asyncio
async def test_streak_reaches_the_risk_engine(session: AsyncSession) -> None:
    for i in range(4):
        session.add(_closed(-100.0, minutes_ago=i, when=datetime.now(UTC)))
    await session.flush()

    mgr = PositionManager(session, settings=get_settings())
    state = await mgr.portfolio_state_input()

    assert state.consecutive_losses == 4
    assert state.cooldown_until is not None
