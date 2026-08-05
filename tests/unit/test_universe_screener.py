"""Liquidity screener unit tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, clear_settings_cache
from app.core.database import Base
import app.models  # noqa: F401
from app.models import MarketSnapshot
from app.universe.screener import evaluate_liquidity, screen_candidates


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


def test_evaluate_liquidity_rejects_thin_name() -> None:
    settings = Settings(
        universe_screener_min_avg_volume=5_000_000,
        universe_screener_max_spread_bps=20,
        universe_screener_min_price=5,
    )
    hit = evaluate_liquidity(
        symbol="THIN",
        last=12.0,
        avg_volume_20d=100_000,
        spread_bps=55,
        settings=settings,
    )
    assert hit.passed is False
    assert "insufficient_volume" in hit.reasons
    assert "excessive_spread" in hit.reasons


def test_evaluate_liquidity_passes_liquid() -> None:
    settings = Settings(
        universe_screener_min_avg_volume=1_000_000,
        universe_screener_max_spread_bps=40,
    )
    hit = evaluate_liquidity(
        symbol="JPM",
        last=200.0,
        avg_volume_20d=10_000_000,
        spread_bps=5,
        settings=settings,
    )
    assert hit.passed is True


@pytest.mark.asyncio
async def test_screen_candidates_uses_db_snapshots(session: AsyncSession) -> None:
    settings = Settings(
        universe_screener_enabled=True,
        universe_screener_min_avg_volume=1_000_000,
        universe_screener_max_spread_bps=30,
        universe_screener_fetch_live=False,
    )
    now = datetime.now(UTC)
    session.add(
        MarketSnapshot(
            id=uuid4(),
            symbol="GOOD",
            as_of=now,
            provider="test",
            last=100.0,
            avg_volume_20d=5_000_000,
            spread_bps=10,
        )
    )
    session.add(
        MarketSnapshot(
            id=uuid4(),
            symbol="BAD",
            as_of=now,
            provider="test",
            last=100.0,
            avg_volume_20d=10_000,
            spread_bps=80,
        )
    )
    await session.flush()
    result = await screen_candidates(session, settings, ["GOOD", "BAD", "MISS"])
    assert "GOOD" in result.passed
    assert any(h.symbol == "BAD" for h in result.rejected)
    # Missing quote kept (offline-friendly) and listed in skipped
    assert "MISS" in result.passed
    assert "MISS" in result.skipped_no_data


@pytest.mark.asyncio
async def test_screener_disabled_passes_all(session: AsyncSession) -> None:
    settings = Settings(universe_screener_enabled=False)
    result = await screen_candidates(session, settings, ["A", "B"])
    assert result.passed == ["A", "B"]
    assert result.source == "disabled"
