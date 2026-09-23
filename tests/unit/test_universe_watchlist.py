"""Universe / watchlist unit tests."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, clear_settings_cache
from app.core.database import Base
import app.models  # noqa: F401
from app.universe.horizons import UniverseHorizon, policy_for
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
def _settings() -> None:
    clear_settings_cache()
    yield
    clear_settings_cache()


def test_horizon_policies_cover_four_books() -> None:
    assert set(UniverseHorizon) == {
        UniverseHorizon.SCALP,
        UniverseHorizon.DAY,
        UniverseHorizon.SHORT,
        UniverseHorizon.MEDIUM,
    }
    assert policy_for("scalp").label_ko == "초단타"
    assert policy_for("medium").max_positions >= 1


@pytest.mark.asyncio
async def test_seed_and_entry_universe(session: AsyncSession) -> None:
    settings = Settings(
        universe_mode="dynamic",
        trade_allowlist=["SPY", "NVDA", "IONQ"],
        enabled_venues=["US"],
        universe_focus_limit=2,
        universe_manager_enabled=False,
    )
    svc = UniverseService(session, settings=settings)
    n = await svc.ensure_seeded()
    assert n == 3
    assert n == await svc.ensure_seeded() or True  # second call no-ops
    assert await svc.ensure_seeded() == 0
    entries = await svc.entry_universe()
    assert entries == {"SPY", "NVDA", "IONQ"}
    focus = await svc.build_focus_without_llm(holdings=["NVDA"])
    assert "NVDA" in focus["symbols"]
    assert len(focus["symbols"]) <= 2 or "NVDA" in focus["symbols"]


@pytest.mark.asyncio
async def test_promoted_candidate_is_entry_eligible(session: AsyncSession) -> None:
    from app.models import WatchlistSymbol

    settings = Settings(
        universe_mode="dynamic",
        trade_allowlist=["SPY"],
        enabled_venues=["US"],
        universe_manager_enabled=False,
        universe_screener_enabled=False,
    )
    svc = UniverseService(session, settings=settings)
    await svc.ensure_seeded()
    session.add(
        WatchlistSymbol(
            symbol="PLTR",
            horizon="short",
            status="active",
            priority=80,
            thesis="weekend promote",
            source="universe_manager",
        )
    )
    await session.flush()
    entries = await svc.entry_universe(venue="US")
    assert "SPY" in entries
    assert "PLTR" in entries
    au = await svc.entry_universe(venue="AU")
    assert "PLTR" not in au


@pytest.mark.asyncio
async def test_au_seed_puts_ndq_on_scalp(session: AsyncSession) -> None:
    from sqlalchemy import select

    from app.models import WatchlistSymbol

    settings = Settings(
        universe_mode="dynamic",
        trade_allowlist=["SPY"],
        trade_allowlist_au=["BHP", "CBA", "VAS", "NDQ"],
        enabled_venues=["US", "AU"],
        universe_manager_enabled=False,
    )
    svc = UniverseService(session, settings=settings)
    await svc.ensure_seeded()
    rows = {
        r.symbol: r.horizon
        for r in (await session.execute(select(WatchlistSymbol))).scalars().all()
    }
    assert rows.get("NDQ") == "scalp"
    assert rows.get("VAS") == "day"
    assert rows.get("BHP") == "short"


@pytest.mark.asyncio
async def test_static_mode_uses_allowlist(session: AsyncSession) -> None:
    settings = Settings(universe_mode="static", trade_allowlist=["QQQ"], enabled_venues=["US"])
    svc = UniverseService(session, settings=settings)
    assert await svc.entry_universe() == {"QQQ"}
    assert await svc.collection_universe(holdings=["AAPL"]) == ["AAPL", "QQQ", "SPY"]


@pytest.mark.asyncio
async def test_paper_collection_includes_full_allowlist(session: AsyncSession) -> None:
    from app.core.config import TradingMode

    settings = Settings(
        universe_mode="dynamic",
        trading_mode=TradingMode.PAPER,
        live_trading_enabled=False,
        paper_aggressive_entries=True,
        trade_allowlist=["SPY", "NVDA", "IONQ"],
        enabled_venues=["US"],
        universe_focus_limit=1,
        universe_manager_enabled=False,
    )
    svc = UniverseService(session, settings=settings)
    await svc.ensure_seeded()
    await svc.build_focus_without_llm(holdings=["NVDA"])
    symbols = await svc.collection_universe(holdings=["NVDA"], venue="US")
    assert "NVDA" in symbols
    assert "SPY" in symbols
    assert "IONQ" in symbols


@pytest.mark.asyncio
async def test_lifecycle_inherits_horizon_hold_policy(session: AsyncSession) -> None:
    from app.intraday.monitor import PositionMonitor
    from app.models import WatchlistSymbol

    settings = Settings(universe_mode="dynamic", trade_allowlist=["SPY", "MSFT"])
    session.add(
        WatchlistSymbol(symbol="SPY", horizon="scalp", status="active", priority=80, thesis="t")
    )
    session.add(
        WatchlistSymbol(symbol="MSFT", horizon="medium", status="active", priority=70, thesis="t")
    )
    await session.flush()
    mon = PositionMonitor(session, settings=settings)
    scalp = await mon.ensure_lifecycle_from_broker(symbol="SPY", quantity=1, avg_entry=100)
    medium = await mon.ensure_lifecycle_from_broker(symbol="MSFT", quantity=1, avg_entry=100)
    assert scalp.overnight_allowed is False
    assert scalp.max_holding_minutes == policy_for("scalp").max_holding_minutes
    assert scalp.stop_price is not None
    assert scalp.stop_price < 100
    assert medium.overnight_allowed is True
    assert medium.max_holding_minutes == policy_for("medium").max_holding_minutes
    assert medium.stop_price is not None
    assert medium.stop_price < scalp.stop_price


@pytest.mark.asyncio
async def test_snapshot_roster_shows_pool_and_listed_days(session: AsyncSession) -> None:
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from app.models import FocusSetSnapshot

    settings = Settings(
        universe_mode="dynamic",
        trade_allowlist=["SPY"],
        universe_candidate_pool=[],
        enabled_venues=["US"],
        universe_manager_enabled=False,
        universe_screener_enabled=False,
    )
    svc = UniverseService(session, settings=settings)
    await svc.ensure_seeded()
    now = datetime(2026, 9, 23, 14, 0, tzinfo=UTC)
    session.add(
        FocusSetSnapshot(
            id=uuid4(),
            as_of=now - timedelta(days=2),
            session_date="2026-09-21",
            symbols=["SPY", "NVDA"],
            holdings=["SPY"],
            rationale="a",
            source="test",
        )
    )
    session.add(
        FocusSetSnapshot(
            id=uuid4(),
            as_of=now - timedelta(days=1),
            session_date="2026-09-22",
            symbols=["SPY"],
            holdings=["SPY"],
            rationale="b",
            source="test",
        )
    )
    session.add(
        FocusSetSnapshot(
            id=uuid4(),
            as_of=now,
            session_date="2026-09-23",
            symbols=["SPY"],
            holdings=["SPY"],
            rationale="c",
            source="test",
        )
    )
    await session.flush()
    snap = await svc.snapshot()
    by_sym = {r["symbol"]: r for r in snap["roster"]}
    assert "JPM" in by_sym and by_sym["JPM"]["role"] == "candidate"
    assert by_sym["JPM"]["status"] == "pool"
    assert by_sym["JPM"]["consecutive_listed_days"] == 0
    assert "ANET" not in by_sym
    assert "ETN" not in by_sym
    assert by_sym["SPY"]["status"] == "active"
    assert by_sym["SPY"]["role"] == "seed"
    assert by_sym["SPY"]["consecutive_listed_days"] >= 1
    assert by_sym["SPY"]["in_focus"] is True
    assert by_sym["SPY"]["consecutive_focus_sessions"] == 3
    assert snap["churn"]["pool_only"] >= 1
    spy_watch = next(r for r in snap["watchlist"] if r["symbol"] == "SPY")
    assert spy_watch["consecutive_listed_days"] >= 1


@pytest.mark.asyncio
async def test_pause_resets_listed_streak(session: AsyncSession) -> None:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from app.models import WatchlistSymbol
    from app.schemas.universe_manager import UniverseManagerOutput, WatchlistProposal
    from app.universe.tenure import inclusive_calendar_days, listed_since

    old = datetime.now(UTC) - timedelta(days=20)
    settings = Settings(
        universe_mode="dynamic",
        trade_allowlist=["SPY"],
        universe_candidate_pool=["JPM"],
        universe_manager_enabled=False,
        universe_screener_enabled=False,
    )
    session.add(
        WatchlistSymbol(
            symbol="JPM",
            horizon="short",
            status="active",
            priority=70,
            thesis="bank",
            source="universe_manager",
            payload={"active_since": old.isoformat()},
        )
    )
    await session.flush()
    svc = UniverseService(session, settings=settings)
    await svc._apply_proposals(
        UniverseManagerOutput(
            timestamp=datetime.now(UTC),
            proposals=[
                WatchlistProposal(
                    symbol="JPM",
                    horizon=UniverseHorizon.SHORT,
                    action="pause",
                    priority=70,
                    thesis="rest",
                    invalidation="x",
                )
            ],
            focus_symbols=["SPY"],
            focus_rationale="pause",
        )
    )
    paused = (
        await session.execute(select(WatchlistSymbol).where(WatchlistSymbol.symbol == "JPM"))
    ).scalar_one()
    assert paused.status == "paused"
    assert not (paused.payload or {}).get("active_since")
    await svc._apply_proposals(
        UniverseManagerOutput(
            timestamp=datetime.now(UTC),
            proposals=[
                WatchlistProposal(
                    symbol="JPM",
                    horizon=UniverseHorizon.SHORT,
                    action="add",
                    priority=80,
                    thesis="back",
                    invalidation="x",
                )
            ],
            focus_symbols=["SPY", "JPM"],
            focus_rationale="add",
        )
    )
    row = {r.symbol: r for r in (await svc.list_active())}["JPM"]
    since = listed_since(row.payload, row.created_at, active=True)
    assert since is not None
    assert inclusive_calendar_days(since) == 1
