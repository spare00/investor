"""Integration: premarket collection against in-memory SQLite."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.database import Base
from app.models import NewsItem  # noqa: F401 — register metadata
from app.services.collection import DataCollectionService


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


@pytest.mark.asyncio
async def test_premarket_collection_persists_and_scores(session: AsyncSession) -> None:
    service = DataCollectionService(session, persist=True)
    bundle = await service.collect_premarket(symbols=["SPY", "QQQ", "NVDA"])
    await session.commit()

    assert bundle.macro is not None
    assert len(bundle.markets) == 3
    assert len(bundle.news) >= 2
    assert bundle.aggregate_quality > 0.5
    # Duplicate headline in stub news should be flagged
    assert any(n.is_duplicate for n in bundle.news)
    assert bundle.fail_closed is False

    from sqlalchemy import func, select

    count = await session.scalar(select(func.count()).select_from(NewsItem))
    assert count is not None and count >= 2


@pytest.mark.asyncio
async def test_collection_fail_closed_on_empty_markets(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.collectors import market_data as md

    class EmptyProvider:
        name = "empty"

        async def fetch_quotes(self, symbols: list[str]) -> list:
            return []

    monkeypatch.setattr(md, "get_market_data_provider", lambda name=None: EmptyProvider())
    # Patch where collection imports it
    import app.services.collection as collection_mod

    monkeypatch.setattr(collection_mod, "get_market_data_provider", lambda name=None: EmptyProvider())

    service = DataCollectionService(session, persist=True)
    bundle = await service.collect_premarket(symbols=["SPY"])
    assert bundle.markets == []
    assert bundle.fail_closed is True
