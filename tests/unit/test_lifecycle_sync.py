"""PositionLifecycle sync from broker positions."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.brokers.mock import MockBroker
from app.core.config import Settings, TradingMode, clear_settings_cache
from app.core.database import Base
import app.models  # noqa: F401
from app.execution.position_manager import PositionManager
from app.execution.safety_controls import trading_controls
from app.intraday.closing import ClosingService
from app.intraday.monitor import PositionMonitor
from app.models import PositionLifecycle, WatchlistSymbol


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
    trading_controls.clear_emergency()
    trading_controls.resume()
    yield
    clear_settings_cache()
    trading_controls.clear_emergency()
    trading_controls.resume()


@pytest.mark.asyncio
async def test_sync_from_broker_positions_upserts_and_closes(session: AsyncSession) -> None:
    mon = PositionMonitor(session, settings=Settings(app_env="test"))
    session.add(
        WatchlistSymbol(symbol="QQQ", horizon="day", status="active", priority=80, thesis="t")
    )
    await session.flush()

    first = await mon.sync_from_broker_positions(
        [
            {
                "symbol": "QQQ",
                "qty": 10,
                "avg_entry_price": 400,
                "market_value": 4100,
            }
        ]
    )
    assert first["upserted"] == 1
    assert first["held"] == ["QQQ:US"]
    rows = list((await session.execute(select(PositionLifecycle))).scalars().all())
    assert len(rows) == 1
    assert rows[0].status == "OPEN"
    assert rows[0].quantity == 10
    assert rows[0].current_price == 410.0
    assert rows[0].overnight_allowed is False  # day book

    second = await mon.sync_from_broker_positions([])
    assert second["closed"] == 1
    await session.refresh(rows[0])
    assert rows[0].status == "CLOSED"
    assert rows[0].quantity == 0


@pytest.mark.asyncio
async def test_sync_reuses_pending_close_lifecycle_by_con_id(session: AsyncSession) -> None:
    """Broker still holding size must not INSERT a second OPEN lifecycle for same con_id."""
    mon = PositionMonitor(session, settings=Settings(app_env="test"))
    con_id = 60009472
    pending = PositionLifecycle(
        id=uuid4(),
        symbol="VAS",
        status="PENDING_CLOSE",
        quantity=876.0,
        average_entry_price=114.1,
        current_price=114.07,
        venue="AU",
        currency="AUD",
        con_id=con_id,
        opened_at=datetime.now(UTC),
        metadata_json={"closed_by": "closing_window"},
    )
    session.add(pending)
    await session.flush()

    out = await mon.sync_from_broker_positions(
        [
            {
                "symbol": "VAS",
                "qty": 876,
                "avg_entry_price": 114.1,
                "market_value": 99925.32,
                "con_id": con_id,
                "exchange": "ASX",
                "currency": "AUD",
            }
        ]
    )
    assert out["upserted"] == 1
    rows = list((await session.execute(select(PositionLifecycle))).scalars().all())
    assert len(rows) == 1
    assert rows[0].id == pending.id
    assert rows[0].status == "OPEN"
    assert rows[0].quantity == 876.0


@pytest.mark.asyncio
async def test_position_manager_sync_creates_lifecycle_for_closing(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        app_env="test",
        trading_mode=TradingMode.PAPER,
        broker_environment="paper",
        broker_provider="mock",
        enable_broker_orders=True,
        enable_automated_execution=True,
        require_manual_order_approval=False,
        auto_execute_force_close=False,
        intraday_operation_mode="PAPER_AUTOMATED",
        default_closing_policy="CLOSE_INTRADAY_ONLY",
        starting_cash=50_000,
    )
    broker = MockBroker(seed=3, starting_cash=50_000, allow_short=False)
    broker.prices["QQQ"] = 400.0
    broker.positions["QQQ"] = {
        "symbol": "QQQ",
        "qty": "5",
        "avg_entry_price": "390",
        "market_value": "2000",
        "unrealized_pl": "50",
        "side": "long",
        "cost_basis": "1950",
    }
    monkeypatch.setattr("app.brokers.factory.get_broker", lambda _s=None: broker)

    session.add(
        WatchlistSymbol(symbol="QQQ", horizon="scalp", status="active", priority=90, thesis="flat")
    )
    await session.flush()

    sync = await PositionManager(session, settings=settings, broker=broker).sync_from_broker()
    assert sync["open_positions"] >= 1
    assert (sync.get("lifecycles") or {}).get("upserted") == 1

    lc = (
        await session.execute(
            select(PositionLifecycle).where(PositionLifecycle.symbol == "QQQ")
        )
    ).scalar_one()
    assert lc.status == "OPEN"

    closing = await ClosingService(session, settings=settings).run_closing()
    assert any(p.get("symbol") == "QQQ" and p.get("action") == "close" for p in closing["plans"])
    assert closing["intent_ids"] or any(
        "pending" in n for n in (closing.get("notes") or [])
    ) or closing.get("intent_drafts")
