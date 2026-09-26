"""A bad broker mark must never become a stored price or a realized P&L.

Regression: a sign-flipped AU mark (-60.70 against a 60.56 entry) was written
straight onto the lifecycle, and the close path then laundered the resulting
unrealized number into +96,765 of realized profit.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.core.config import clear_settings_cache, get_settings
from app.core.database import Base
from app.intraday.monitor import PositionMonitor
from app.intraday.pnl import stamp_lifecycle_close_pnl, usable_mark
from app.models import PositionLifecycle


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


def _lifecycle(**overrides: object) -> PositionLifecycle:
    defaults: dict[str, object] = {
        "id": uuid4(),
        "symbol": "BHP",
        "status": "OPEN",
        "quantity": -798.0,
        "average_entry_price": 60.56,
        "current_price": 60.50,
        "venue": "AU",
        "currency": "AUD",
        "realized_pl": 0.0,
    }
    defaults.update(overrides)
    return PositionLifecycle(**defaults)


@pytest.mark.parametrize("bad", [-60.70, 0.0, 1e12, float("nan"), float("inf"), None])
def test_usable_mark_rejects_broker_sentinels(bad: float | None) -> None:
    assert usable_mark(bad) is None


def test_usable_mark_falls_through_to_first_sane_candidate() -> None:
    assert usable_mark(-60.70, 0.0, 60.56) == pytest.approx(60.56)


@pytest.mark.asyncio
async def test_monitor_does_not_store_sign_flipped_mark(session: AsyncSession) -> None:
    lc = _lifecycle()
    session.add(lc)
    await session.flush()

    monitor = PositionMonitor(session, settings=get_settings())
    result = await monitor.evaluate(lc, current_price=-60.70, equity=976_000.0)

    assert "mark_rejected" in result.reasons
    # Falls back to the last good mark, never the flipped one.
    assert lc.current_price == pytest.approx(60.50)
    assert abs(float(lc.unrealized_pl or 0.0)) < 100.0


@pytest.mark.asyncio
async def test_monitor_flags_when_no_mark_is_usable(session: AsyncSession) -> None:
    lc = _lifecycle(current_price=None, average_entry_price=None)
    session.add(lc)
    await session.flush()

    monitor = PositionMonitor(session, settings=get_settings())
    result = await monitor.evaluate(lc, current_price=0.0, equity=976_000.0)

    assert "mark_unusable" in result.reasons
    assert lc.current_price is None


def test_close_does_not_launder_a_bad_mark_into_realized_pnl() -> None:
    # The exact shape of the corrupt BHP row before the guard existed.
    lc = _lifecycle(current_price=-60.70, unrealized_pl=96_765.48)
    assert stamp_lifecycle_close_pnl(lc) == 0.0
    assert lc.metadata_json["pnl_unavailable"] == "no_usable_mark"


def test_close_still_prices_a_normal_exit() -> None:
    lc = _lifecycle(
        symbol="GOOGL", quantity=100.0, average_entry_price=338.56, current_price=349.20
    )
    assert stamp_lifecycle_close_pnl(lc) == pytest.approx(1064.0, abs=0.01)
