"""Session residue fold + no CIO committee after the venue close."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, TradingMode
from app.core.database import Base
from app.execution.safety_controls import trading_controls
from app.intraday.session_hygiene import committee_allowed_for_phase, fold_session_residue
from app.models import AlertRecordModel, IntradayEvent, OrderIntent, PositionLifecycle
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
    trading_controls.clear_emergency()
    trading_controls.resume()
    yield
    trading_controls.clear_emergency()
    trading_controls.resume()


def test_committee_only_during_regular() -> None:
    assert committee_allowed_for_phase("REGULAR", in_force_close=False, in_closing=False) is True
    assert committee_allowed_for_phase("REGULAR", in_force_close=True, in_closing=False) is False
    assert committee_allowed_for_phase("AFTER_HOURS", in_force_close=False, in_closing=False) is False
    assert committee_allowed_for_phase("POSTMARKET", in_force_close=False, in_closing=False) is False


@pytest.mark.asyncio
async def test_fold_expires_orphan_hard_stop_and_cba_event(session: AsyncSession) -> None:
    now = datetime(2026, 8, 21, 3, 45, tzinfo=UTC)
    session.add(
        PositionLifecycle(
            id=uuid4(),
            symbol="BHP",
            status="OPEN",
            quantity=1575,
            average_entry_price=63,
            current_price=65,
            venue="AU",
            exit_policy={"horizon": "short"},
        )
    )
    session.add(
        IntradayEvent(
            id=uuid4(),
            event_type="STOP_TRIGGERED",
            source="test",
            symbols=["CBA"],
            importance="critical",
            detected_at=now - timedelta(hours=20),
            expires_at=now - timedelta(hours=14),
            deduplication_key="stop:CBA:old",
            status="NEW",
            requires_risk_review=True,
        )
    )
    session.add(
        IntradayEvent(
            id=uuid4(),
            event_type="MARKET_CLOSED",
            source="test",
            symbols=[],
            importance="medium",
            detected_at=now - timedelta(hours=8),
            expires_at=now + timedelta(hours=1),
            deduplication_key="closed:AU:old",
            status="NEW",
        )
    )
    session.add(
        OrderIntent(
            id=uuid4(),
            symbol="CBA",
            intent_type="exit",
            side="sell",
            quantity=575,
            status="CREATED",
            thesis="hard_stop",
            metadata_json={"reason": "hard_stop"},
        )
    )
    session.add(
        AlertRecordModel(
            id=uuid4(),
            severity="critical",
            alert_type="trading.hard_stop",
            title="trading.hard_stop",
            message="Hard stop triggered on CBA (orders submitted)",
            detected_at=now - timedelta(hours=20),
            deduplication_key="hard_stop:CBA:2026-08-20",
            status="active",
            payload={"context": {"symbol": "CBA", "submitted": True}},
        )
    )
    await session.flush()
    out = await fold_session_residue(
        session, now=now, phase="REGULAR", session_date="2026-08-21"
    )
    assert out["events"] >= 2
    assert out["intents"] == 1
    assert out["alerts"] == 1
    evs = list((await session.execute(select(IntradayEvent))).scalars().all())
    assert all(e.status != "NEW" for e in evs)
    intent = (await session.execute(select(OrderIntent))).scalar_one()
    assert intent.status == "EXPIRED"
    alert = (await session.execute(select(AlertRecordModel))).scalar_one()
    assert alert.status == "resolved"


@pytest.mark.asyncio
async def test_evaluate_intraday_skips_committee_after_hours(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = {"n": 0}

    async def _blocked(*_a: object, **_k: object) -> dict:
        called["n"] += 1
        return {"skipped": False, "portfolio_action": "HOLD"}

    monkeypatch.setattr("app.intraday.agents.IntradayAgentService.evaluate", _blocked)
    settings = Settings(
        app_env="test",
        trading_mode=TradingMode.PAPER,
        broker_environment="paper",
        enable_broker_orders=False,
        enable_intraday_monitoring=False,
        enable_intraday_agent_reanalysis=True,
        enable_scheduler=False,
        intraday_operation_mode="OBSERVE_ONLY",
    )
    svc = DailyWorkflowService(session, settings=settings)
    await svc.prepare(session_date="2026-08-03")
    run = await svc.get_current("2026-08-03")
    assert run is not None
    run.current_state = DailyWorkflowState.INTRADAY.value
    await session.flush()
    out = await svc.evaluate_intraday(
        session_date="2026-08-03",
        trigger="interval",
        now=datetime(2026, 8, 4, 1, 0, tzinfo=UTC),
        fake_llm=True,
    )
    assert called["n"] == 0
    assert "committee_skipped_phase" in str((out.get("intraday") or {}).get("reason") or "")
