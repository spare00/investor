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
async def test_hard_stop_submits_when_paper_automated_flag_off(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _armed()
    settings = settings.model_copy(update={"auto_execute_hard_stops": False})
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
        intraday_operation_mode="MANUAL_APPROVAL",
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


@pytest.mark.asyncio
async def test_hard_stop_emits_ops_alert(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.alerts.fake_provider import FakeAlertProvider
    from app.alerts.service import AlertService
    from sqlalchemy import select
    from app.models import AlertRecordModel

    settings = Settings(
        app_env="test",
        trading_mode=TradingMode.PAPER,
        broker_environment="paper",
        enable_broker_orders=True,
        enable_automated_execution=True,
        require_manual_order_approval=False,
        auto_execute_hard_stops=False,
        enable_intraday_monitoring=True,
        enable_alerts=True,
        alert_provider="fake",
        critical_alert_cooldown_seconds=0,
        intraday_operation_mode="MANUAL_APPROVAL",
        starting_cash=50_000.0,
    )
    provider = FakeAlertProvider()
    real_init = AlertService.__init__

    def _init(self, session=None, settings=None, provider_arg=None):  # type: ignore[no-untyped-def]
        real_init(self, session, settings=settings, provider=provider)

    monkeypatch.setattr(AlertService, "__init__", _init)

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
    assert any(a.code == "trading.hard_stop" for a in provider.sent)
    persisted = list((await session.execute(select(AlertRecordModel))).scalars().all())
    assert any(a.alert_type == "trading.hard_stop" for a in persisted)


@pytest.mark.asyncio
async def test_monitor_emergency_emits_alert(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.alerts.fake_provider import FakeAlertProvider
    from app.alerts.service import AlertService

    settings = Settings(
        app_env="test",
        trading_mode=TradingMode.PAPER,
        enable_intraday_monitoring=True,
        enable_alerts=True,
        alert_provider="fake",
        critical_alert_cooldown_seconds=0,
        daily_max_loss_pct=1.0,
        max_drawdown_pct=50.0,
        starting_cash=50_000.0,
        intraday_operation_mode="OBSERVE_ONLY",
    )
    provider = FakeAlertProvider()
    real_init = AlertService.__init__

    def _init(self, session=None, settings=None, provider_arg=None):  # type: ignore[no-untyped-def]
        real_init(self, session, settings=settings, provider=provider)

    monkeypatch.setattr(AlertService, "__init__", _init)

    session.add(
        PositionLifecycle(
            id=uuid4(),
            symbol="QQQ",
            status="OPEN",
            quantity=1,
            average_entry_price=100,
            current_price=100,
            overnight_allowed=True,
            exit_policy={},
        )
    )
    # PortfolioSnapshot with huge daily loss so monitor hits daily_loss_limit
    from app.models import PortfolioSnapshot
    from datetime import UTC, datetime

    session.add(
        PortfolioSnapshot(
            id=uuid4(),
            as_of=datetime.now(UTC),
            equity=40_000,
            cash=40_000,
            cash_pct=100,
            gross_exposure_pct=0,
            daily_pnl=-2000,
            daily_pnl_pct=-5.0,
            drawdown_pct=0.0,
            open_positions=1,
        )
    )
    await session.flush()
    rows = await IntradayService(session, settings=settings).monitor_all(prices={"QQQ": 100.0})
    hit = next(r for r in rows if r["symbol"] == "QQQ")
    assert hit["monitor"]["verdict"] == "EMERGENCY_ACTION_REQUIRED"
    assert any(a.code == "trading.monitor_emergency" for a in provider.sent)


@pytest.mark.asyncio
async def test_hard_stop_exit_intent_stamps_venue(session: AsyncSession) -> None:
    from uuid import UUID

    from app.models import OrderIntent

    settings = Settings(
        app_env="test",
        trading_mode=TradingMode.PAPER,
        broker_environment="paper",
        enable_intraday_monitoring=True,
        enable_broker_orders=False,
        auto_execute_hard_stops=False,
        intraday_operation_mode="MANUAL_APPROVAL",
    )
    session.add(
        PositionLifecycle(
            id=uuid4(),
            symbol="BHP",
            status="OPEN",
            quantity=2,
            average_entry_price=40,
            current_price=40,
            stop_price=39,
            overnight_allowed=True,
            venue="AU",
            con_id=4242,
            exit_policy={},
        )
    )
    await session.flush()
    rows = await IntradayService(session, settings=settings).monitor_all(
        prices={"BHP": 38.0}, venue="AU"
    )
    hit = next(r for r in rows if r["symbol"] == "BHP")
    assert hit.get("exit_intent_id")
    intent = await session.get(OrderIntent, UUID(hit["exit_intent_id"]))
    assert intent is not None
    meta = intent.metadata_json or {}
    assert meta.get("venue") == "AU"
    assert meta.get("con_id") == 4242


@pytest.mark.asyncio
async def test_monitor_all_scopes_to_venue(session: AsyncSession) -> None:
    settings = Settings(
        app_env="test",
        trading_mode=TradingMode.PAPER,
        broker_environment="paper",
        enable_intraday_monitoring=True,
        enable_broker_orders=False,
        auto_execute_hard_stops=False,
    )
    session.add(
        PositionLifecycle(
            id=uuid4(),
            symbol="SPY",
            status="OPEN",
            quantity=1,
            average_entry_price=100,
            current_price=100,
            stop_price=95,
            overnight_allowed=True,
            venue="US",
            exit_policy={},
        )
    )
    session.add(
        PositionLifecycle(
            id=uuid4(),
            symbol="BHP",
            status="OPEN",
            quantity=1,
            average_entry_price=40,
            current_price=40,
            stop_price=35,
            overnight_allowed=True,
            venue="AU",
            con_id=999,
            exit_policy={},
        )
    )
    await session.flush()
    us_rows = await IntradayService(session, settings=settings).monitor_all(
        prices={"SPY": 94.0, "BHP": 34.0}, venue="US"
    )
    assert [r["symbol"] for r in us_rows if not r.get("skipped")] == ["SPY"]
    au_rows = await IntradayService(session, settings=settings).monitor_all(
        prices={"SPY": 94.0, "BHP": 34.0}, venue="AU"
    )
    assert [r["symbol"] for r in au_rows if not r.get("skipped")] == ["BHP"]


@pytest.mark.asyncio
async def test_max_holding_submits_when_armed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    settings = _armed()
    broker = MockBroker(seed=7, starting_cash=50_000, allow_short=False)
    broker.prices["QQQ"] = 400.0
    monkeypatch.setattr("app.brokers.factory.get_broker", lambda _s=None: broker)
    monkeypatch.setattr("app.execution.order_manager.get_broker", lambda _s=None: broker)
    session.add(
        PositionLifecycle(
            id=uuid4(),
            symbol="QQQ",
            status="OPEN",
            quantity=10,
            average_entry_price=400,
            current_price=400,
            stop_price=None,
            overnight_allowed=False,
            max_holding_minutes=60,
            opened_at=datetime.now(UTC) - timedelta(hours=8),
            venue="US",
            exit_policy={"horizon": "day"},
        )
    )
    await session.flush()
    rows = await IntradayService(session, settings=settings).monitor_all(prices={"QQQ": 400.0})
    hit = next(r for r in rows if r["symbol"] == "QQQ")
    assert "max_holding_time" in (hit["monitor"]["reasons"] or [])
    assert hit.get("exit_intent_id")
    assert hit.get("orders_submitted", 0) >= 1
