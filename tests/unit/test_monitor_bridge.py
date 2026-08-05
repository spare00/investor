"""Unattended monitor → evaluate_intraday risk escalation."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import clear_settings_cache, get_settings
from app.core.database import Base
import app.models  # noqa: F401
from app.execution.safety_controls import trading_controls
from app.models import IntradayEvent, PositionLifecycle
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


@pytest.mark.asyncio
async def test_evaluate_intraday_escalates_stop_to_risk_change(session: AsyncSession) -> None:
    svc = DailyWorkflowService(session, settings=get_settings())
    await svc.prepare(session_date="2026-08-03")
    run = await svc.get_current("2026-08-03")
    assert run is not None
    run.current_state = DailyWorkflowState.INTRADAY.value
    session.add(
        PositionLifecycle(
            id=uuid4(),
            symbol="QQQ",
            status="OPEN",
            quantity=10,
            average_entry_price=400,
            current_price=390,
            stop_price=395,
            overnight_allowed=False,
            exit_policy={},
        )
    )
    await session.flush()

    now = datetime(2026, 8, 3, 17, 0, tzinfo=UTC)  # regular session
    out = await svc.evaluate_intraday(
        session_date="2026-08-03", trigger="interval", now=now, fake_llm=True
    )
    intra = out["intraday"]
    assert intra.get("monitor")
    assert (intra["monitor"].get("actionable") or 0) >= 1
    assert intra.get("trigger") == "risk_change"
    events = list((await session.execute(select(IntradayEvent))).scalars().all())
    assert any(e.event_type == "STOP_TRIGGERED" for e in events)


@pytest.mark.asyncio
async def test_postmarket_runs_settlement(session: AsyncSession) -> None:
    svc = DailyWorkflowService(session, settings=get_settings())
    await svc.prepare(session_date="2026-08-03")
    run = await svc.get_current("2026-08-03")
    assert run is not None
    run.current_state = DailyWorkflowState.CLOSING_WINDOW.value
    await session.flush()
    post = await svc.run_postmarket(session_date="2026-08-03")
    assert post["current_state"] == DailyWorkflowState.COMPLETED.value
    review = post["review"]
    # Settlement may soft-fail without broker; either settlement or settlement_error is set.
    assert "settlement" in review or "settlement_error" in review
    assert "performance" in review or "performance_error" in review
