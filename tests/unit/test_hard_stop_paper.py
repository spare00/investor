"""Hard-stop paper auto-submit when armed."""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.brokers.mock import MockBroker
from app.core.config import Settings, TradingMode, clear_settings_cache
from app.core.database import Base
import app.models  # noqa: F401
from app.execution.safety_controls import trading_controls
from app.intraday.service import IntradayService
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
    trading_controls.clear_emergency()
    trading_controls.resume()
    yield
    clear_settings_cache()
    trading_controls.clear_emergency()
    trading_controls.resume()


def _armed() -> Settings:
    return Settings(
        app_env="test",
        trading_mode=TradingMode.PAPER,
        broker_environment="paper",
        broker_provider="mock",
        enable_live_trading=False,
        enable_broker_orders=True,
        enable_automated_execution=True,
        require_manual_order_approval=False,
        auto_execute_hard_stops=True,
        enable_intraday_monitoring=True,
        intraday_operation_mode="PAPER_AUTOMATED",
        starting_cash=50_000.0,
    )


@pytest.mark.asyncio
async def test_hard_stop_submits_when_armed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _armed()
    broker = MockBroker(seed=7, starting_cash=50_000, allow_short=False)
    broker.prices["SPY"] = 95.0
    monkeypatch.setattr("app.brokers.factory.get_broker", lambda _s=None: broker)
    monkeypatch.setattr("app.execution.order_manager.get_broker", lambda _s=None: broker)

    session.add(
        PositionLifecycle(
            id=uuid4(),
            symbol="SPY",
            status="OPEN",
            quantity=10,
            average_entry_price=100,
            current_price=95,
            stop_price=98,
            overnight_allowed=False,
            exit_policy={},
        )
    )
    await session.flush()

    rows = await IntradayService(session, settings=settings).monitor_all(prices={"SPY": 95.0})
    hit = next(r for r in rows if r["symbol"] == "SPY")
    assert hit["stop"]["triggered"] is True
    assert hit.get("exit_intent_id")
    assert hit.get("orders_submitted", 0) >= 1


@pytest.mark.asyncio
async def test_hard_stop_pending_when_not_armed(session: AsyncSession) -> None:
    settings = Settings(
        app_env="test",
        trading_mode=TradingMode.PAPER,
        broker_environment="paper",
        enable_broker_orders=True,
        enable_automated_execution=True,
        require_manual_order_approval=False,
        auto_execute_hard_stops=False,
        enable_intraday_monitoring=True,
        intraday_operation_mode="PAPER_AUTOMATED",
        starting_cash=50_000.0,
    )
    session.add(
        PositionLifecycle(
            id=uuid4(),
            symbol="SPY",
            status="OPEN",
            quantity=10,
            average_entry_price=100,
            current_price=95,
            stop_price=98,
            overnight_allowed=False,
            exit_policy={},
        )
    )
    await session.flush()
    rows = await IntradayService(session, settings=settings).monitor_all(prices={"SPY": 95.0})
    hit = next(r for r in rows if r["symbol"] == "SPY")
    assert hit.get("exit_intent_id")
    assert hit.get("orders_submitted") in (None, 0)
    assert "hard_stop_intent_pending_submit" in (hit.get("notes") or [])
