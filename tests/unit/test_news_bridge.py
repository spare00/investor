"""News → intraday bus → evaluate_intraday escalation tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import clear_settings_cache, get_settings
from app.core.database import Base
import app.models  # noqa: F401
from app.execution.safety_controls import trading_controls
from app.intraday.news_bridge import classify_news_importance, ingest_high_importance_news
from app.models import IntradayEvent, NewsItem
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


def _news(
    *,
    headline: str,
    category: str | None = None,
    importance: str | None = None,
    published_at: datetime | None = None,
    symbols: list[str] | None = None,
) -> NewsItem:
    now = published_at or datetime.now(UTC)
    payload = {"importance": importance} if importance else {}
    return NewsItem(
        id=uuid4(),
        provider="test",
        external_id=str(uuid4()),
        headline=headline,
        headline_hash=str(uuid4()).replace("-", "")[:32],
        source="unit",
        published_at=now,
        collected_at=now,
        symbols=symbols or ["NVDA"],
        category=category,
        raw_payload=payload,
        is_duplicate=False,
    )


def test_classify_news_importance_from_payload_and_headline() -> None:
    assert (
        classify_news_importance(_news(headline="Quiet tape", importance="critical")) == "critical"
    )
    assert classify_news_importance(_news(headline="Quiet tape", category="earnings")) == "high"
    assert classify_news_importance(_news(headline="FOMC holds rates steady")) == "high"
    assert classify_news_importance(_news(headline="Chip makers see steady demand")) is None


@pytest.mark.asyncio
async def test_ingest_publishes_and_dedupes(session: AsyncSession) -> None:
    settings = get_settings()
    now = datetime(2026, 8, 5, 15, 0, tzinfo=UTC)
    session.add(_news(headline="NVDA beats earnings", published_at=now - timedelta(minutes=5)))
    session.add(_news(headline="Routine product update", published_at=now - timedelta(minutes=3)))
    await session.flush()

    first = await ingest_high_importance_news(session, settings=settings, now=now)
    assert first["published"] == 1
    assert first["scanned"] == 2
    events = list((await session.execute(select(IntradayEvent))).scalars().all())
    assert len(events) == 1
    assert events[0].event_type == "HIGH_IMPORTANCE_NEWS"
    assert events[0].requires_analysis is True
    assert events[0].status == "NEW"

    second = await ingest_high_importance_news(session, settings=settings, now=now)
    assert second["published"] == 0  # deduped within window
    await session.refresh(events[0])
    assert events[0].status == "NEW"  # still actionable
    assert int((events[0].payload or {}).get("dedupe_hits") or 0) >= 1


@pytest.mark.asyncio
async def test_evaluate_intraday_escalates_on_high_news(session: AsyncSession) -> None:
    svc = DailyWorkflowService(session, settings=get_settings())
    await svc.prepare(session_date="2026-08-03")
    run = await svc.get_current("2026-08-03")
    assert run is not None
    run.current_state = DailyWorkflowState.INTRADAY.value
    now = datetime(2026, 8, 3, 17, 0, tzinfo=UTC)
    session.add(
        _news(
            headline="Fed signals emergency rate cut",
            importance="critical",
            published_at=now - timedelta(minutes=10),
            symbols=["SPY"],
        )
    )
    await session.flush()

    out = await svc.evaluate_intraday(
        session_date="2026-08-03", trigger="interval", now=now, fake_llm=True
    )
    intra = out["intraday"]
    assert intra.get("news", {}).get("published", 0) >= 1
    assert intra.get("trigger") == "news_high_importance"


@pytest.mark.asyncio
async def test_evaluate_intraday_no_false_news_escalate(session: AsyncSession) -> None:
    """Regression: routine news must not force news_high_importance."""
    svc = DailyWorkflowService(session, settings=get_settings())
    await svc.prepare(session_date="2026-08-03")
    run = await svc.get_current("2026-08-03")
    assert run is not None
    run.current_state = DailyWorkflowState.INTRADAY.value
    now = datetime(2026, 8, 3, 17, 0, tzinfo=UTC)
    session.add(
        _news(
            headline="Analyst maintains market-weight view",
            published_at=now - timedelta(minutes=5),
        )
    )
    await session.flush()

    out = await svc.evaluate_intraday(
        session_date="2026-08-03", trigger="interval", now=now, fake_llm=True
    )
    intra = out["intraday"]
    assert (intra.get("news") or {}).get("published", 0) == 0
    assert intra.get("trigger") == "interval"
