"""Force-close paper auto-submit path (mock broker)."""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.brokers.mock import MockBroker
from app.core.config import Settings, TradingMode, clear_settings_cache
from app.core.database import Base
import app.models  # noqa: F401
from app.execution.safety_controls import TradingControls, trading_controls
from app.intraday.closing import ClosingService
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
def _reset_controls() -> None:
    clear_settings_cache()
    trading_controls.clear_emergency()
    trading_controls.resume()
    yield
    clear_settings_cache()
    trading_controls.clear_emergency()
    trading_controls.resume()


def _armed_settings() -> Settings:
    return Settings(
        app_env="test",
        trading_mode=TradingMode.PAPER,
        broker_environment="paper",
        broker_provider="mock",
        enable_live_trading=False,
        enable_broker_orders=True,
        enable_automated_execution=True,
        require_manual_order_approval=False,
        auto_execute_force_close=True,
        intraday_operation_mode="PAPER_AUTOMATED",
        default_closing_policy="CLOSE_INTRADAY_ONLY",
    )


@pytest.mark.asyncio
async def test_force_close_submits_paper_when_armed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _armed_settings()
    broker = MockBroker(seed=11, starting_cash=50_000, allow_short=False)
    broker.prices["QQQ"] = 400.0
    broker.positions["QQQ"] = {
        "symbol": "QQQ",
        "qty": "10",
        "avg_entry_price": "390",
        "market_value": "4000",
        "unrealized_pl": "100",
        "side": "long",
    }

    session.add(
        WatchlistSymbol(symbol="QQQ", horizon="day", status="active", priority=80, thesis="flatten")
    )
    session.add(
        PositionLifecycle(
            id=uuid4(),
            symbol="QQQ",
            status="OPEN",
            quantity=10,
            average_entry_price=390,
            current_price=400,
            overnight_allowed=False,
            exit_policy={},
        )
    )
    await session.flush()

    import app.execution.order_manager as om_mod

    monkeypatch.setattr(om_mod, "get_broker", lambda settings=None: broker)

    from sqlalchemy import select

    from app.models import Order

    closing = await ClosingService(session, settings=settings).run_closing()
    assert closing["intent_ids"]
    assert closing["broker_orders_submitted"] is True
    assert closing["orders_submitted"] >= 1
    orders = list((await session.execute(select(Order))).scalars().all())
    assert len(orders) >= 1
    assert orders[0].symbol == "QQQ"
    assert orders[0].side == "sell"


@pytest.mark.asyncio
async def test_force_close_intent_only_when_not_armed(session: AsyncSession) -> None:
    settings = Settings(
        app_env="test",
        trading_mode=TradingMode.PAPER,
        broker_environment="paper",
        enable_broker_orders=True,
        enable_automated_execution=True,
        require_manual_order_approval=False,
        auto_execute_force_close=False,
        intraday_operation_mode="PAPER_AUTOMATED",
        default_closing_policy="CLOSE_INTRADAY_ONLY",
    )
    session.add(
        WatchlistSymbol(symbol="QQQ", horizon="scalp", status="active", priority=80, thesis="t")
    )
    session.add(
        PositionLifecycle(
            id=uuid4(),
            symbol="QQQ",
            status="OPEN",
            quantity=5,
            average_entry_price=400,
            current_price=400,
            overnight_allowed=False,
            exit_policy={},
        )
    )
    await session.flush()
    closing = await ClosingService(session, settings=settings).run_closing()
    assert closing["intent_ids"]
    assert closing["broker_orders_submitted"] is False
    assert "force_close_intents_pending_submit" in closing["notes"]


@pytest.mark.asyncio
async def test_closing_skips_duplicate_pending_close(session: AsyncSession) -> None:
    settings = Settings(
        app_env="test",
        trading_mode=TradingMode.PAPER,
        broker_environment="paper",
        enable_broker_orders=True,
        enable_automated_execution=True,
        require_manual_order_approval=False,
        auto_execute_force_close=False,
        intraday_operation_mode="PAPER_AUTOMATED",
        default_closing_policy="CLOSE_INTRADAY_ONLY",
    )
    session.add(
        WatchlistSymbol(symbol="QQQ", horizon="scalp", status="active", priority=80, thesis="t")
    )
    session.add(
        PositionLifecycle(
            id=uuid4(),
            symbol="QQQ",
            status="OPEN",
            quantity=5,
            average_entry_price=400,
            current_price=400,
            overnight_allowed=False,
            exit_policy={},
        )
    )
    await session.flush()
    first = await ClosingService(session, settings=settings).run_closing()
    assert first["intent_ids"]
    second = await ClosingService(session, settings=settings).run_closing()
    assert second["intent_ids"] == []
    assert any("skip_duplicate_close:QQQ" in n for n in second["notes"])


@pytest.mark.asyncio
async def test_daily_start_closing_materializes_intents(session: AsyncSession) -> None:
    """Unattended scheduler path: DailyWorkflowService.start_closing → ClosingService."""
    from app.workflow.daily import DailyWorkflowService
    from app.workflow.states import DailyWorkflowState

    settings = Settings(
        app_env="test",
        trading_mode=TradingMode.PAPER,
        broker_environment="paper",
        enable_broker_orders=True,
        enable_automated_execution=True,
        require_manual_order_approval=False,
        auto_execute_force_close=False,
        intraday_operation_mode="PAPER_AUTOMATED",
        default_closing_policy="CLOSE_INTRADAY_ONLY",
        enable_scheduler=False,
    )
    svc = DailyWorkflowService(session, settings=settings)
    await svc.prepare(session_date="2026-08-03")
    run = await svc.get_current("2026-08-03")
    assert run is not None
    run.current_state = DailyWorkflowState.INTRADAY.value
    # prepare() seeds QQQ as scalp/day — attach an open lifecycle for flatten.
    session.add(
        PositionLifecycle(
            id=uuid4(),
            symbol="QQQ",
            status="OPEN",
            quantity=8,
            average_entry_price=400,
            current_price=405,
            overnight_allowed=False,
            exit_policy={},
        )
    )
    await session.flush()

    out = await svc.start_closing(session_date="2026-08-03")
    assert out["current_state"] == DailyWorkflowState.CLOSING_WINDOW.value
    closing = out["closing"]
    assert closing["broker_orders_allowed"] is False
    assert closing["intent_ids"]
    assert closing["broker_orders_submitted"] is False
    assert any(p.get("action") == "close" for p in (closing.get("plans") or []))
