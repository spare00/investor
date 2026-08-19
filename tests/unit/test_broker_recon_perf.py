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


@pytest.mark.asyncio
async def test_recon_adopts_remote_only_order(session: AsyncSession) -> None:
    from datetime import UTC, datetime

    from app.brokers.base import OrderResult, OrderStatus

    settings = Settings(app_env="test", broker_provider="mock", enable_broker_connection=True)
    broker = MockBroker(starting_cash=25_000)
    broker.orders["46"] = OrderResult(
        broker_order_id="46",
        status=OrderStatus.ACCEPTED,
        submitted_at=datetime.now(UTC),
        raw={"symbol": "VAS", "side": "sell", "qty": 400, "order_type": "market"},
    )
    svc = ReconciliationService(session, settings=settings)
    svc.broker = broker
    result = await svc.run("ON_DEMAND")
    assert result["adopted_orders"] == 1
    assert result["result"] == "IN_SYNC"
    assert result["blocks_new_orders"] is False
    row = (await session.execute(select(Order).where(Order.broker_order_id == "46"))).scalar_one()
    assert row.symbol == "VAS"
    assert row.side == "sell"
    assert row.raw_payload.get("adopted") is True
    again = await svc.run("ON_DEMAND")
    assert again["adopted_orders"] == 0
    assert again["result"] == "IN_SYNC"


@pytest.mark.asyncio
async def test_recon_empty_remote_book_is_not_material(session: AsyncSession) -> None:
    settings = Settings(app_env="test", broker_provider="mock", enable_broker_connection=True)
    session.add(
        Order(
            id=uuid4(),
            symbol="VAS",
            side="sell",
            qty=400,
            order_type="market",
            status="ACCEPTED",
            broker_order_id="42",
            idempotency_key=f"local-{uuid4()}",
        )
    )
    await session.flush()
    svc = ReconciliationService(session, settings=settings)
    svc.broker = MockBroker(starting_cash=25_000)
    book = BrokerBook(orders=[], positions=[], account={"cash": 1})
    result = await svc.run("ON_DEMAND", book=book)
    assert result["result"] == "MINOR_DRIFT"
    assert result["blocks_new_orders"] is False
    assert result["issues"][0]["type"] == "empty_remote_open_orders"


@pytest.mark.asyncio
async def test_recon_clears_local_opens_after_empty_remote_streak(
    session: AsyncSession,
) -> None:
    settings = Settings(app_env="test", broker_provider="mock", enable_broker_connection=True)
    session.add(
        Order(
            id=uuid4(),
            symbol="VAS",
            side="sell",
            qty=400,
            order_type="market",
            status="ACCEPTED",
            broker_order_id="42",
            idempotency_key=f"local-{uuid4()}",
        )
    )
    await session.flush()
    svc = ReconciliationService(session, settings=settings)
    empty = BrokerBook(orders=[], positions=[], account={"cash": 1})
    first = await svc.run("ON_DEMAND", book=empty)
    second = await svc.run("ON_DEMAND", book=empty)
    assert first["result"] == "MINOR_DRIFT"
    assert second["result"] == "MINOR_DRIFT"
    third = await svc.run("ON_DEMAND", book=empty)
    assert third["issues"][0]["type"] == "stale_local_open_cleared"
    assert third["issues"][0]["closed"] == 1
    assert third["result"] == "MINOR_DRIFT"
    row = (await session.execute(select(Order).where(Order.broker_order_id == "42"))).scalar_one()
    assert row.status == "CANCELLED"


@pytest.mark.asyncio
async def test_reap_stale_running_jobs(session: AsyncSession) -> None:
    from datetime import UTC, datetime, timedelta

    from app.core.scheduler import _reap_stale_running_jobs
    from app.models import ScheduledJobRecord

    settings = Settings(app_env="test", job_action_timeout_seconds_local=60, llm_runtime="local")
    now = datetime.now(UTC)
    session.add(
        ScheduledJobRecord(
            job_key="AU:intraday_eval_5",
            session_date="2026-08-19",
            planned_at=now - timedelta(hours=2),
            started_at=now - timedelta(hours=1),
            status="running",
        )
    )
    await session.flush()
    n = await _reap_stale_running_jobs(session, settings, now)
    assert n == 1
    row = (
        await session.execute(
            select(ScheduledJobRecord).where(ScheduledJobRecord.job_key == "AU:intraday_eval_5")
        )
    ).scalar_one()
    assert row.status == "failed"
    assert row.error and row.error.startswith("stale_running_reaped")
