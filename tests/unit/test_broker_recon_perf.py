"""Broker recon / sync performance helpers."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.brokers.mock import MockBroker
from app.core.config import Settings
from app.core.database import Base
from app.execution.position_manager import PositionManager
from app.execution.reconciliation import BrokerBook, ReconciliationService
from app.intraday.broker_updates import BrokerUpdateProcessor
from app.models import Order, PortfolioSnapshot
from uuid import uuid4


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
async def test_sync_skips_unchanged_snapshot(session: AsyncSession) -> None:
    settings = Settings(app_env="test", broker_provider="mock", starting_cash=50_000)
    broker = MockBroker(starting_cash=50_000)
    pm = PositionManager(session, settings=settings, broker=broker)
    first = await pm.sync_from_broker()
    assert first["snapshot_written"] is True
    second = await pm.sync_from_broker(account=broker.account, positions=list(broker.positions.values()))
    assert second["snapshot_written"] is False
    snaps = list((await session.execute(select(PortfolioSnapshot))).scalars().all())
    assert len(snaps) == 1


@pytest.mark.asyncio
async def test_poll_skips_unchanged_order_status(session: AsyncSession) -> None:
    settings = Settings(app_env="test", broker_provider="mock")
    broker = MockBroker(starting_cash=25_000)
    row = Order(
        id=uuid4(),
        symbol="QQQ",
        side="buy",
        qty=1,
        order_type="market",
        status="ACCEPTED",
        broker_order_id="mock-1",
        idempotency_key=f"test-{uuid4()}",
    )
    session.add(row)
    await session.flush()

    class Remote:
        broker_order_id = "mock-1"
        status = type("S", (), {"value": "accepted"})()
        filled_qty = 0
        avg_fill_price = None
        submitted_at = None

    proc = BrokerUpdateProcessor(session, settings=settings)
    proc.broker = broker
    out = await proc.poll_and_apply(remote_orders=[Remote()])
    assert out["updated"] == 0
    assert out["skipped_unchanged"] == 1


@pytest.mark.asyncio
async def test_recon_shared_book_avoids_refetch(session: AsyncSession) -> None:
    settings = Settings(app_env="test", broker_provider="mock", enable_broker_connection=True)
    broker = MockBroker(starting_cash=25_000)
    svc = ReconciliationService(session, settings=settings)
    svc.broker = broker
    book = await svc.fetch_book()
    assert isinstance(book, BrokerBook)
    result = await svc.run("SCHEDULED", book=book)
    assert result["book"] is book
    assert result["result"] in {"IN_SYNC", "MATERIAL_DRIFT", "MINOR_DRIFT"}
