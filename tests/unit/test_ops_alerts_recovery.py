"""Operational alert emitters + intraday recovery lifecycle sync."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.alerts.base import AlertSeverity
from app.alerts.ops import (
    emit_emergency_stop_alert,
    emit_llm_budget_alert,
    emit_reconciliation_alert,
)
from app.brokers.mock import MockBroker
from app.core.config import Settings, TradingMode, clear_settings_cache
from app.core.database import Base
import app.models  # noqa: F401
from app.execution.ops_persistence import persist_trading_controls
from app.execution.safety_controls import TradingControls, trading_controls
from app.intraday.recovery import IntradayRecoveryService
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


@pytest.fixture
def settings() -> Settings:
    return Settings(
        app_env="test",
        enable_alerts=True,
        alert_provider="fake",
        critical_alert_cooldown_seconds=0,
        warning_alert_cooldown_seconds=0,
        broker_provider="mock",
        broker_environment="paper",
        trading_mode=TradingMode.PAPER,
        enable_broker_orders=True,
        enable_broker_connection=True,
    )


@pytest.mark.asyncio
async def test_recon_alert_only_on_bad_results(settings: Settings) -> None:
    ok = await emit_reconciliation_alert(None, settings, result="MATCH", sync_type="T")
    assert ok is None

    drift = await emit_reconciliation_alert(
        None, settings, result="MATERIAL_DRIFT", issues=[{"x": 1}], sync_type="SCHEDULED"
    )
    assert drift is not None and drift.emitted is True
    assert drift.alert is not None
    assert drift.alert.severity == AlertSeverity.CRITICAL

    unavail = await emit_reconciliation_alert(
        None, settings, result="BROKER_UNAVAILABLE", sync_type="RECOVERY"
    )
    assert unavail is not None and unavail.emitted is True
    assert unavail.alert is not None
    assert unavail.alert.severity == AlertSeverity.WARNING


@pytest.mark.asyncio
async def test_recon_in_sync_auto_resolves_open_alerts(
    session: AsyncSession, settings: Settings
) -> None:
    from app.models import AlertRecordModel

    drift = await emit_reconciliation_alert(
        session, settings, result="MATERIAL_DRIFT", issues=[{"x": 1}], sync_type="SCHEDULED"
    )
    assert drift is not None and drift.emitted is True and drift.alert_id is not None
    row = await session.get(AlertRecordModel, drift.alert_id)
    assert row is not None
    assert row.status == "active"
    assert row.alert_type == "recon.material_drift"

    cleared = await emit_reconciliation_alert(session, settings, result="IN_SYNC", sync_type="SCHEDULED")
    assert cleared is None
    await session.refresh(row)
    assert row.status == "resolved"


@pytest.mark.asyncio
async def test_emergency_and_llm_budget_alerts(settings: Settings) -> None:
    emergency = await emit_emergency_stop_alert(
        None, settings, reason="operator", source="test"
    )
    assert emergency is not None and emergency.emitted is True

    soft = await emit_llm_budget_alert(
        settings=settings,
        code="llm.budget_soft_limit",
        message="soft",
        severity=AlertSeverity.WARNING,
    )
    assert soft is not None and soft.emitted is True

    hard = await emit_llm_budget_alert(
        settings=settings,
        code="llm.budget_exhausted",
        message="hard",
        severity=AlertSeverity.CRITICAL,
    )
    assert hard is not None and hard.emitted is True


@pytest.mark.asyncio
async def test_intraday_recovery_syncs_lifecycles_and_alerts(
    session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = MockBroker(seed=9, starting_cash=50_000)
    broker.prices["SPY"] = 100.0
    broker.positions["SPY"] = {
        "symbol": "SPY",
        "qty": "3",
        "avg_entry_price": "99",
        "market_value": "300",
        "unrealized_pl": "3",
        "side": "long",
        "cost_basis": "297",
    }
    monkeypatch.setattr("app.brokers.factory.get_broker", lambda _s=None: broker)
    monkeypatch.setattr("app.execution.position_manager.get_broker", lambda _s=None: broker)
    monkeypatch.setattr("app.execution.reconciliation.get_broker", lambda _s=None: broker)
    monkeypatch.setattr("app.intraday.broker_updates.get_broker", lambda _s=None: broker)

    session.add(
        WatchlistSymbol(symbol="SPY", horizon="day", status="active", priority=50, thesis="t")
    )
    await session.flush()

    controls = TradingControls()
    controls.emergency_stop("boot_test")
    await persist_trading_controls(session, controls)
    # Align process-global controls with persisted emergency (recovery restores into trading_controls)
    trading_controls.emergency_stop("boot_test")

    result = await IntradayRecoveryService(session, settings=settings).run()
    assert result["emergency_stop"] is True
    assert result["new_orders_allowed"] is False
    assert any(a.startswith("lifecycles_upserted:") for a in result["actions"])

    lc = (
        await session.execute(select(PositionLifecycle).where(PositionLifecycle.symbol == "SPY"))
    ).scalar_one()
    assert lc.status == "OPEN"
    assert lc.quantity == 3
