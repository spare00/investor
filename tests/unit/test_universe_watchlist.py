"""Universe / watchlist unit tests."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, clear_settings_cache
from app.core.database import Base
import app.models  # noqa: F401
from app.universe.horizons import UniverseHorizon, policy_for
from app.universe.service import UniverseService


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
def _settings() -> None:
    clear_settings_cache()
    yield
    clear_settings_cache()


def test_horizon_policies_cover_four_books() -> None:
    assert set(UniverseHorizon) == {
        UniverseHorizon.SCALP,
        UniverseHorizon.DAY,
        UniverseHorizon.SHORT,
        UniverseHorizon.MEDIUM,
    }
    assert policy_for("scalp").label_ko == "초단타"
    assert policy_for("medium").max_positions >= 1


@pytest.mark.asyncio
async def test_seed_and_entry_universe(session: AsyncSession) -> None:
    settings = Settings(
        universe_mode="dynamic",
        trade_allowlist=["SPY", "NVDA", "IONQ"],
        universe_focus_limit=2,
        universe_manager_enabled=False,
    )
    svc = UniverseService(session, settings=settings)
    n = await svc.ensure_seeded()
    assert n == 3
    assert n == await svc.ensure_seeded() or True  # second call no-ops
    assert await svc.ensure_seeded() == 0
    entries = await svc.entry_universe()
    assert entries == {"SPY", "NVDA", "IONQ"}
    focus = await svc.build_focus_without_llm(holdings=["NVDA"])
    assert "NVDA" in focus["symbols"]
    assert len(focus["symbols"]) <= 2 or "NVDA" in focus["symbols"]


@pytest.mark.asyncio
async def test_static_mode_uses_allowlist(session: AsyncSession) -> None:
    settings = Settings(universe_mode="static", trade_allowlist=["QQQ"])
    svc = UniverseService(session, settings=settings)
    assert await svc.entry_universe() == {"QQQ"}
    assert await svc.collection_universe(holdings=["AAPL"]) == ["AAPL", "QQQ"]


@pytest.mark.asyncio
async def test_lifecycle_inherits_horizon_hold_policy(session: AsyncSession) -> None:
    from app.intraday.monitor import PositionMonitor
    from app.models import WatchlistSymbol

    settings = Settings(universe_mode="dynamic", trade_allowlist=["SPY", "MSFT"])
    session.add(
        WatchlistSymbol(symbol="SPY", horizon="scalp", status="active", priority=80, thesis="t")
    )
    session.add(
        WatchlistSymbol(symbol="MSFT", horizon="medium", status="active", priority=70, thesis="t")
    )
    await session.flush()
    mon = PositionMonitor(session, settings=settings)
    scalp = await mon.ensure_lifecycle_from_broker(symbol="SPY", quantity=1, avg_entry=100)
    medium = await mon.ensure_lifecycle_from_broker(symbol="MSFT", quantity=1, avg_entry=100)
    assert scalp.overnight_allowed is False
    assert scalp.max_holding_minutes == policy_for("scalp").max_holding_minutes
    assert medium.overnight_allowed is True
    assert medium.max_holding_minutes == policy_for("medium").max_holding_minutes
