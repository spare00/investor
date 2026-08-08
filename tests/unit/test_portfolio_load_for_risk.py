"""PositionManager.load_for_risk — broker book for risk/agents."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.brokers.errors import BrokerError
from app.brokers.mock import MockBroker
from app.core.config import Settings
from app.core.database import Base
from app.execution.position_manager import PositionManager


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


@pytest.mark.asyncio
async def test_load_for_risk_syncs_broker_when_orders_armed(session: AsyncSession) -> None:
    settings = Settings(
        app_env="test",
        enable_broker_orders=True,
        enable_automated_execution=True,
        enable_broker_connection=True,
        broker_provider="mock",
        starting_cash=100_000.0,
    )
    broker = MockBroker(starting_cash=80_000.0)
    broker.positions["AAPL"] = {
        "symbol": "AAPL",
        "qty": "10",
        "avg_entry_price": "150",
        "market_value": "1600",
        "cost_basis": "1500",
        "unrealized_pl": "100",
    }
    broker.account["equity"] = "81600"
    broker.account["cash"] = "80000"
    broker.account["portfolio_value"] = "81600"

    pm = PositionManager(session, settings=settings, broker=broker)
    state, note = await pm.load_for_risk()
    assert note == "portfolio_from_broker"
    assert abs(state.equity - 81600.0) < 1e-6
    assert abs(state.cash - 80000.0) < 1e-6
    assert any(p.symbol == "AAPL" for p in state.positions)


@pytest.mark.asyncio
async def test_load_for_risk_fails_closed_when_armed_and_broker_down(session: AsyncSession) -> None:
    settings = Settings(
        app_env="test",
        enable_broker_orders=True,
        enable_automated_execution=True,
        enable_broker_connection=True,
        broker_provider="mock",
        starting_cash=100_000.0,
    )

    class BoomBroker(MockBroker):
        async def get_account(self) -> dict[str, object]:
            raise BrokerError("broker_down")

        async def get_positions(self) -> list[dict[str, object]]:
            raise BrokerError("broker_down")

    pm = PositionManager(session, settings=settings, broker=BoomBroker())
    with pytest.raises(BrokerError):
        await pm.load_for_risk()


@pytest.mark.asyncio
async def test_load_for_risk_falls_back_when_unarmed(session: AsyncSession) -> None:
    settings = Settings(
        app_env="test",
        enable_broker_orders=False,
        enable_automated_execution=False,
        enable_broker_connection=True,
        broker_provider="mock",
        starting_cash=100_000.0,
    )

    class BoomBroker(MockBroker):
        async def get_account(self) -> dict[str, object]:
            raise BrokerError("broker_down")

        async def get_positions(self) -> list[dict[str, object]]:
            raise BrokerError("broker_down")

    pm = PositionManager(session, settings=settings, broker=BoomBroker())
    state, note = await pm.load_for_risk()
    assert note == "portfolio_db_fallback"
    assert state.equity == 100_000.0
    assert state.positions == []
