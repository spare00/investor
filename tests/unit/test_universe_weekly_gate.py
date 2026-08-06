"""Weekly Universe Manager LLM gate — protect intraday trading budget."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, clear_settings_cache
from app.core.database import Base
import app.models  # noqa: F401
from app.models.entities import FocusSetSnapshot
from app.schemas.universe_manager import UniverseManagerOutput
from app.universe.service import UniverseService


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
def _settings_cache() -> None:
    clear_settings_cache()
    yield
    clear_settings_cache()


class _StubAgent:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, payload: object) -> UniverseManagerOutput:
        self.calls += 1
        return UniverseManagerOutput(
            timestamp=datetime.now(UTC),
            proposals=[],
            focus_symbols=["SPY", "QQQ"],
            focus_rationale="stub",
            notes=["stub"],
        )


async def _seed_llm_focus(session: AsyncSession, *, days_ago: int) -> None:
    session.add(
        FocusSetSnapshot(
            id=uuid4(),
            as_of=datetime.now(UTC) - timedelta(days=days_ago),
            session_date="2026-08-01",
            symbols=["SPY"],
            holdings=[],
            rationale="prior",
            source="universe_manager",
            payload={},
        )
    )
    await session.commit()


@pytest.mark.asyncio
async def test_refresh_skips_llm_within_weekly_window(session: AsyncSession) -> None:
    await _seed_llm_focus(session, days_ago=2)
    settings = Settings(
        universe_manager_enabled=True,
        universe_mode="dynamic",
        universe_refresh_min_interval_days=7,
        trade_allowlist=["SPY", "QQQ"],
    )
    agent = _StubAgent()
    svc = UniverseService(session, settings=settings, agent=agent)  # type: ignore[arg-type]
    result = await svc.refresh(holdings=[])
    assert result["skipped"] is True
    assert result["reason"] == "min_interval"
    assert agent.calls == 0


@pytest.mark.asyncio
async def test_refresh_runs_llm_when_week_elapsed(session: AsyncSession) -> None:
    await _seed_llm_focus(session, days_ago=8)
    settings = Settings(
        universe_manager_enabled=True,
        universe_mode="dynamic",
        universe_refresh_min_interval_days=7,
        trade_allowlist=["SPY", "QQQ"],
        universe_screener_enabled=False,
    )
    agent = _StubAgent()
    svc = UniverseService(session, settings=settings, agent=agent)  # type: ignore[arg-type]
    result = await svc.refresh(holdings=[])
    assert result["skipped"] is False
    assert agent.calls == 1


@pytest.mark.asyncio
async def test_refresh_force_bypasses_weekly_gate(session: AsyncSession) -> None:
    await _seed_llm_focus(session, days_ago=1)
    settings = Settings(
        universe_manager_enabled=True,
        universe_mode="dynamic",
        universe_refresh_min_interval_days=7,
        trade_allowlist=["SPY", "QQQ"],
        universe_screener_enabled=False,
    )
    agent = _StubAgent()
    svc = UniverseService(session, settings=settings, agent=agent)  # type: ignore[arg-type]
    result = await svc.refresh(holdings=[], force=True)
    assert result["skipped"] is False
    assert agent.calls == 1
