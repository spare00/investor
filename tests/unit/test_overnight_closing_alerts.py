"""Overnight review in daily closing path + alert resolve-by-code."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.alerts.base import AlertSeverity
from app.alerts.ops import (
    emit_emergency_stop_alert,
    emit_overnight_review_alert,
    resolve_alerts_by_code,
)
from app.alerts.service import AlertService
from app.core.config import Settings, TradingMode, clear_settings_cache
from app.core.database import Base
import app.models  # noqa: F401
from app.execution.safety_controls import trading_controls
from app.market.calendar import MarketCalendarService
from app.models import AlertRecordModel, OvernightReview, PositionLifecycle
from app.workflow.daily import DailyWorkflowService
from app.workflow.states import DailyWorkflowState


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


def test_holiday_gap_detects_weekday_closed() -> None:
    cal = MarketCalendarService(Settings(app_env="test"))
    # 2026-07-02 (Thu) → next session 2026-07-06 after Jul 3 Independence Day observed
    assert cal.next_session_has_holiday_gap(date(2026, 7, 2)) is True
    # Normal mid-week: 2026-08-03 Mon → Tue, no holiday
    assert cal.next_session_has_holiday_gap(date(2026, 8, 3)) is False


@pytest.mark.asyncio
async def test_start_closing_runs_overnight_review(session: AsyncSession) -> None:
    settings = Settings(
        app_env="test",
        trading_mode=TradingMode.PAPER,
        broker_environment="paper",
        enable_broker_orders=True,
        enable_automated_execution=True,
        require_manual_order_approval=False,
        auto_execute_force_close=False,
        overnight_review_required=True,
        enable_alerts=True,
        alert_provider="fake",
        critical_alert_cooldown_seconds=0,
        warning_alert_cooldown_seconds=0,
        intraday_operation_mode="MANUAL_APPROVAL",
        default_closing_policy="CLOSE_INTRADAY_ONLY",
        enable_scheduler=False,
    )
    svc = DailyWorkflowService(session, settings=settings)
    await svc.prepare(session_date="2026-08-03")
    run = await svc.get_current("2026-08-03")
    assert run is not None
    run.current_state = DailyWorkflowState.INTRADAY.value
    session.add(
        PositionLifecycle(
            id=uuid4(),
            symbol="QQQ",
            status="OPEN",
            quantity=5,
            average_entry_price=400,
            current_price=405,
            overnight_allowed=False,
            exit_policy={},
        )
    )
    await session.flush()

    out = await svc.start_closing(session_date="2026-08-03")
    overnight = (out.get("closing") or {}).get("overnight_review") or {}
    reviews = overnight.get("reviews") or []
    assert reviews
    assert any(r["symbol"] == "QQQ" for r in reviews)
    rows = list((await session.execute(select(OvernightReview))).scalars().all())
    assert rows
    assert any(r.symbol == "QQQ" for r in rows)


@pytest.mark.asyncio
async def test_overnight_alert_and_resolve_emergency_by_code(session: AsyncSession) -> None:
    settings = Settings(
        app_env="test",
        enable_alerts=True,
        alert_provider="fake",
        critical_alert_cooldown_seconds=0,
        warning_alert_cooldown_seconds=0,
    )
    flagged = await emit_overnight_review_alert(
        session,
        settings,
        reviews=[
            {"symbol": "AAPL", "status": "CLOSE_BEFORE_MARKET_CLOSE", "reasons": ["horizon_day"]}
        ],
        session_date="2026-08-03",
    )
    assert flagged is not None and flagged.emitted is True

    ok = await emit_overnight_review_alert(
        session,
        settings,
        reviews=[{"symbol": "MSFT", "status": "OVERNIGHT_APPROVED", "reasons": []}],
        session_date="2026-08-04",
    )
    assert ok is None

    await emit_emergency_stop_alert(session, settings, reason="test", source="test")
    n = await resolve_alerts_by_code(session, settings, code="trading.emergency_stop")
    assert n >= 1
    rows = list(
        (
            await session.execute(
                select(AlertRecordModel).where(
                    AlertRecordModel.alert_type == "trading.emergency_stop"
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows
    assert all(r.status == "resolved" for r in rows)


@pytest.mark.asyncio
async def test_alert_ack_resolve_from_db(session: AsyncSession) -> None:
    settings = Settings(
        app_env="test",
        enable_alerts=True,
        alert_provider="fake",
        critical_alert_cooldown_seconds=0,
    )
    svc = AlertService(session, settings=settings)
    emitted = await svc.emit(
        code="trading.hard_stop",
        message="test",
        severity=AlertSeverity.CRITICAL,
    )
    assert emitted.emitted and emitted.alert_id
    # Simulate fresh service (no in-memory) — ack/resolve via DB
    fresh = AlertService(session, settings=settings)
    ack = await fresh.acknowledge(emitted.alert_id, by="tester")
    assert ack.emitted is True
    resolved = await fresh.resolve(emitted.alert_id)
    assert resolved.emitted is True
    row = await session.get(AlertRecordModel, emitted.alert_id)
    assert row is not None
    assert row.status == "resolved"
