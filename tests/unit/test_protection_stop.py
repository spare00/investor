"""A position is only protected when its own stop is actually resting.

Regression: `_submit_protection_stop` treated *any* working same-side order as
protection, so a take-profit limit or another lifecycle's order on the same
dual-listed ticker was enough to mark the row protected with nothing standing
between it and a gap. 86 of 259 lifecycles never got a stop, and no stop order
ever filled.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.core.config import Settings
from app.core.database import Base
from app.intraday.monitor import PositionMonitor
from app.intraday.service import IntradayService
from app.models import Order, PositionLifecycle

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
        "average_entry_price": 338.56,
        "current_price": 340.00,
        "stop_price": 333.00,
        "protection_submitted": False,
        "opened_at": NOW,
    }
    defaults.update(overrides)
    return PositionLifecycle(**defaults)


def _order(**overrides: object) -> Order:
    defaults: dict[str, object] = {
        "id": uuid4(),
        "idempotency_key": f"k-{uuid4()}",
        "symbol": "GOOGL",
        "side": "sell",
        "qty": 100.0,
        "order_type": "stop",
        "stop_price": 333.00,
        "status": "submitted",
    }
    defaults.update(overrides)
    return Order(**defaults)


@pytest.mark.asyncio
async def test_a_short_position_is_reported_as_unprotected(session: AsyncSession) -> None:
    """`qty > 0` meant the entire short book never asked for a stop."""
    lc = _lifecycle(symbol="BHP", quantity=-798.0, average_entry_price=60.56, stop_price=62.50)
    session.add(lc)
    await session.flush()

    monitor = PositionMonitor(session, settings=_settings())
    result = await monitor.evaluate(lc, current_price=60.70, equity=976_000.0)

    assert "protection_order_missing" in result.reasons


@pytest.mark.asyncio
async def test_a_long_position_is_still_reported(session: AsyncSession) -> None:
    lc = _lifecycle()
    session.add(lc)
    await session.flush()

    monitor = PositionMonitor(session, settings=_settings())
    result = await monitor.evaluate(lc, current_price=340.0, equity=100_000.0)

    assert "protection_order_missing" in result.reasons


@pytest.mark.asyncio
async def test_a_take_profit_limit_does_not_count_as_protection(session: AsyncSession) -> None:
    lc = _lifecycle()
    session.add(lc)
    # Same symbol, same side, working — but a target, not a stop.
    session.add(_order(order_type="limit", stop_price=None, limit_price=350.0))
    await session.flush()

    svc = IntradayService(session, settings=_settings())
    await svc._submit_protection_stop(lc)

    assert lc.protection_submitted is False


@pytest.mark.asyncio
async def test_another_lifecycles_stop_does_not_count(session: AsyncSession) -> None:
    lc = _lifecycle()
    other = _lifecycle()
    session.add_all([lc, other])
    session.add(_order(idempotency_key=f"protect-stop:{other.id}:333.0000"))
    await session.flush()

    svc = IntradayService(session, settings=_settings())
    await svc._submit_protection_stop(lc)

    assert lc.protection_submitted is False


@pytest.mark.asyncio
async def test_its_own_resting_stop_does_count(session: AsyncSession) -> None:
    lc = _lifecycle()
    session.add(lc)
    session.add(_order(idempotency_key=f"protect-stop:{lc.id}:333.0000"))
    await session.flush()

    svc = IntradayService(session, settings=_settings())
    submitted = await svc._submit_protection_stop(lc)

    assert submitted == 0
    assert lc.protection_submitted is True


@pytest.mark.asyncio
async def test_mixed_case_rows_still_match(session: AsyncSession) -> None:
    """The orders table holds both `stop`/`stp` and `FILLED`/`filled`."""
    lc = _lifecycle()
    session.add(lc)
    session.add(
        _order(
            idempotency_key=f"protect-stop:{lc.id}:333.0000",
            order_type="STP",
            status="SUBMITTED",
        )
    )
    await session.flush()

    svc = IntradayService(session, settings=_settings())
    await svc._submit_protection_stop(lc)

    assert lc.protection_submitted is True


@pytest.mark.asyncio
async def test_a_stop_left_behind_by_the_trail_is_not_treated_as_current(
    session: AsyncSession,
) -> None:
    """Breakeven trail moved the stop to entry; the resting order is stale."""
    lc = _lifecycle(stop_price=338.56)
    session.add(lc)
    session.add(_order(idempotency_key=f"protect-stop:{lc.id}:333.0000", stop_price=333.00))
    await session.flush()

    svc = IntradayService(session, settings=_settings())
    await svc._submit_protection_stop(lc)

    # Not silently accepted as protection at the outgrown level.
    assert lc.protection_submitted is False
