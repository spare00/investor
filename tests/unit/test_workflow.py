"""Phase 5 workflow tests."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.database import Base
from app.decision.workflow import WorkflowService
from app.execution.safety_controls import TradingControls, trading_controls
from app.services.llm import StubLLMClient

ET = ZoneInfo("America/New_York")


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
def _reset_trading_controls() -> None:
    # Reset singleton to active between tests.
    trading_controls.clear_emergency("test_reset")
    trading_controls.resume("test_reset")
    yield
    trading_controls.clear_emergency("test_reset")
    trading_controls.resume("test_reset")


@pytest.mark.asyncio
async def test_premarket_workflow_validates(session: AsyncSession) -> None:
    svc = WorkflowService(session, llm=StubLLMClient({}), persist=True)
    result = await svc.run_premarket()
    assert result.kind == "premarket"
    assert result.collection is not None
    assert result.analysis is not None
    assert result.validation is not None
    assert result.analysis.cio.hard_veto_honored is True


@pytest.mark.asyncio
async def test_intraday_respects_min_interval(session: AsyncSession) -> None:
    svc = WorkflowService(session, llm=StubLLMClient({}), persist=False)
    first = await svc.run_intraday_evaluate(
        force=True, now=datetime(2026, 8, 3, 12, 0, tzinfo=ET)
    )
    assert first.skipped_reason is None
    second = await svc.run_intraday_evaluate(
        force=False, now=datetime(2026, 8, 3, 12, 1, tzinfo=ET)
    )
    assert second.skipped_reason == "min_reeval_interval"


@pytest.mark.asyncio
async def test_intraday_skips_outside_session(session: AsyncSession) -> None:
    svc = WorkflowService(session, llm=StubLLMClient({}), persist=False)
    result = await svc.run_intraday_evaluate(
        force=False, now=datetime(2026, 8, 3, 7, 0, tzinfo=ET)
    )
    assert result.skipped_reason == "outside_regular_session"


@pytest.mark.asyncio
async def test_postmarket_produces_no_entry_intents(session: AsyncSession) -> None:
    svc = WorkflowService(session, llm=StubLLMClient({}), persist=True)
    result = await svc.run_postmarket()
    assert result.kind == "postmarket"
    assert result.validation is not None
    assert result.validation.intents == []
