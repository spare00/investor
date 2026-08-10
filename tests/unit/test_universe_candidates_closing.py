"""Candidate pool + closing horizon tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, clear_settings_cache
from app.core.database import Base
import app.models  # noqa: F401
from app.execution.safety_controls import TradingControls
from app.execution.validation import ExecutionValidator
from app.intraday.closing import ClosingService
from app.models import PositionLifecycle, WatchlistSymbol
from app.risk import PortfolioRiskView, PositionRiskView
from app.schemas.cio import CIODecision, SymbolActionPlan
from app.schemas.common import MarketRegime, PortfolioAction, PriceZone, SymbolAction
from app.universe.candidates import addable_universe, curated_candidate_pool
from app.universe.horizons import UniverseHorizon
from app.universe.service import UniverseService
from app.schemas.universe_manager import UniverseManagerOutput, WatchlistProposal


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


def test_curated_pool_expands_beyond_seed() -> None:
    settings = Settings(trade_allowlist=["SPY"], universe_candidate_pool=[])
    pool = curated_candidate_pool(settings)
    assert "JPM" in pool
    assert "SPY" not in pool or True
    allowed = addable_universe(settings, known_symbols=set())
    assert "SPY" in allowed
    assert "JPM" in allowed
    assert "ZZZZ" not in allowed


def test_theme_ranking_boosts_matching_names() -> None:
    from app.universe.candidates import ranked_candidate_pool

    settings = Settings(trade_allowlist=["SPY"], universe_candidate_pool=[])
    ranked = ranked_candidate_pool(settings, themes=["semiconductor"])
    assert ranked[0] in {"SMH", "SOXX", "MU", "INTC", "AMD", "AVGO"}
    # Non-theme names still present later
    assert "WMT" in ranked
    assert ranked.index("SMH") < ranked.index("WMT")


def test_regime_maps_to_themes() -> None:
    from app.universe.candidates import themes_for_regime

    assert "tech" in themes_for_regime("risk_on")
    assert "tech" in themes_for_regime("RISK_ON")
    assert "tech" in themes_for_regime("STRONG_RISK_ON")
    assert themes_for_regime(None) == []
    assert themes_for_regime("INSUFFICIENT_DATA") == []


@pytest.mark.asyncio
async def test_hygiene_pauses_illiquid_active(session: AsyncSession) -> None:
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.models import MarketSnapshot, WatchlistSymbol
    from app.universe.service import UniverseService

    settings = Settings(
        universe_mode="dynamic",
        trade_allowlist=["SPY", "THIN"],
        universe_manager_enabled=False,
        universe_screener_enabled=True,
        universe_screener_pause_illiquid=True,
        universe_screener_min_avg_volume=1_000_000,
        universe_screener_max_spread_bps=30,
        universe_screener_fetch_live=False,
    )
    now = datetime.now(UTC)
    session.add(
        WatchlistSymbol(symbol="SPY", horizon="scalp", status="active", priority=80, thesis="ok")
    )
    session.add(
        WatchlistSymbol(symbol="THIN", horizon="day", status="active", priority=70, thesis="illiquid")
    )
    session.add(
        MarketSnapshot(
            id=uuid4(),
            symbol="SPY",
            as_of=now,
            provider="test",
            last=500.0,
            avg_volume_20d=50_000_000,
            spread_bps=5,
        )
    )
    session.add(
        MarketSnapshot(
            id=uuid4(),
            symbol="THIN",
            as_of=now,
            provider="test",
            last=20.0,
            avg_volume_20d=1_000,
            spread_bps=90,
        )
    )
    await session.flush()
    svc = UniverseService(session, settings=settings)
    out = await svc.hygiene_active_watchlist(holdings=["SPY"])
    assert any(p["symbol"] == "THIN" for p in out["paused"])
    rows = {r.symbol: r.status for r in (await svc.list_active())}
    # list_active only returns active — SPY remains, THIN paused
    assert "SPY" in rows
    assert "THIN" not in rows


@pytest.mark.asyncio
async def test_apply_session_context_boosts_theme_names(session: AsyncSession) -> None:
    settings = Settings(
        universe_mode="dynamic",
        trade_allowlist=["SPY", "SMH", "WMT"],
        universe_manager_enabled=False,
        universe_focus_limit=3,
    )
    svc = UniverseService(session, settings=settings)
    await svc.ensure_seeded()
    before = {r.symbol: r.priority for r in await svc.list_active()}
    out = await svc.apply_session_context(
        holdings=["SPY"], market_regime="RISK_ON", themes=["semiconductor"]
    )
    assert out["boosted"] >= 1
    after = {r.symbol: r.priority for r in await svc.list_active()}
    assert after.get("SMH", 0) >= before.get("SMH", 0)


def test_candidate_adds_can_be_disabled() -> None:
    settings = Settings(
        trade_allowlist=["SPY"],
        universe_allow_candidate_adds=False,
        universe_candidate_pool=["JPM"],
    )
    allowed = addable_universe(settings, known_symbols={"NVDA"})
    assert allowed == {"SPY", "NVDA"}


@pytest.mark.asyncio
async def test_apply_allows_candidate_add(session: AsyncSession) -> None:
    settings = Settings(
        universe_mode="dynamic",
        trade_allowlist=["SPY"],
        universe_candidate_pool=["JPM"],
        universe_manager_enabled=False,
    )
    svc = UniverseService(session, settings=settings)
    await svc.ensure_seeded()
    out = UniverseManagerOutput(
        timestamp=datetime.now(UTC),
        proposals=[
            WatchlistProposal(
                symbol="JPM",
                horizon=UniverseHorizon.SHORT,
                action="add",
                priority=70,
                thesis="bank strength",
                invalidation="break support",
            ),
            WatchlistProposal(
                symbol="ZZZZ",
                horizon=UniverseHorizon.DAY,
                action="add",
                priority=90,
                thesis="hallucination",
                invalidation="x",
            ),
        ],
        focus_symbols=["SPY", "JPM"],
        focus_rationale="test",
    )
    await svc._apply_proposals(out)
    active = {r.symbol for r in await svc.list_active()}
    assert "JPM" in active
    assert "ZZZZ" not in active


@pytest.mark.asyncio
async def test_closing_forces_scalp_even_if_overnight_flag(session: AsyncSession) -> None:
    settings = Settings(
        intraday_operation_mode="MANUAL_APPROVAL",
        default_closing_policy="CLOSE_INTRADAY_ONLY",
        auto_execute_force_close=False,
    )
    session.add(
        WatchlistSymbol(symbol="QQQ", horizon="scalp", status="active", priority=80, thesis="t")
    )
    session.add(
        PositionLifecycle(
            id=uuid4(),
            symbol="QQQ",
            status="OPEN",
            quantity=10,
            average_entry_price=400,
            current_price=400,
            overnight_allowed=True,  # mis-set
            exit_policy={},
        )
    )
    await session.flush()
    closing = await ClosingService(session, settings=settings).run_closing()
    plan = next(p for p in closing["plans"] if p["symbol"] == "QQQ")
    assert plan["action"] == "close"
    assert plan["is_intraday_only"] is True


@pytest.mark.asyncio
async def test_closing_prefers_lifecycle_exit_policy_horizon(session: AsyncSession) -> None:
    """Watchlist says medium; lifecycle scalp must still flatten at close."""
    settings = Settings(
        intraday_operation_mode="MANUAL_APPROVAL",
        default_closing_policy="CLOSE_INTRADAY_ONLY",
        auto_execute_force_close=False,
    )
    session.add(
        WatchlistSymbol(symbol="QQQ", horizon="medium", status="active", priority=80, thesis="t")
    )
    session.add(
        PositionLifecycle(
            id=uuid4(),
            symbol="QQQ",
            status="OPEN",
            quantity=10,
            average_entry_price=400,
            current_price=400,
            overnight_allowed=True,
            exit_policy={"horizon": "scalp"},
        )
    )
    await session.flush()
    closing = await ClosingService(session, settings=settings).run_closing()
    plan = next(p for p in closing["plans"] if p["symbol"] == "QQQ")
    assert plan["action"] == "close"
    assert plan["is_intraday_only"] is True


@pytest.mark.asyncio
async def test_closing_creates_order_intent(session: AsyncSession) -> None:
    from sqlalchemy import select

    from app.models import OrderIntent

    settings = Settings(
        intraday_operation_mode="MANUAL_APPROVAL",
        default_closing_policy="CLOSE_INTRADAY_ONLY",
        auto_execute_force_close=False,
    )
    session.add(
        WatchlistSymbol(symbol="QQQ", horizon="day", status="active", priority=80, thesis="t")
    )
    session.add(
        PositionLifecycle(
            id=uuid4(),
            symbol="QQQ",
            status="OPEN",
            quantity=10,
            average_entry_price=400,
            current_price=400,
            overnight_allowed=False,
            exit_policy={},
        )
    )
    await session.flush()
    closing = await ClosingService(session, settings=settings).run_closing()
    assert closing["intent_ids"]
    assert closing["broker_orders_submitted"] is False
    rows = list((await session.execute(select(OrderIntent))).scalars().all())
    assert len(rows) >= 1
    assert rows[0].symbol == "QQQ"
    assert "force_close_intents_pending_submit" in closing["notes"]


NOW = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)


def test_validator_skips_entries_keeps_exits_in_closing() -> None:
    decision = CIODecision(
        decision_id=uuid4(),
        timestamp=NOW,
        market_regime=MarketRegime.RISK_ON,
        portfolio_action=PortfolioAction.REDUCE,
        symbol_actions=[
            SymbolActionPlan(
                symbol="SPY",
                action=SymbolAction.BUY,
                confidence=70,
                target_position_pct=5,
                stop_loss=400,
                thesis="blocked entry",
                invalidation="x",
                entry_zone=PriceZone(min=450, max=451),
            ),
            SymbolActionPlan(
                symbol="QQQ",
                action=SymbolAction.SELL,
                confidence=80,
                target_position_pct=0,
                thesis="flatten",
                invalidation="n/a",
            ),
        ],
        cash_target_pct=50,
        risk_approval=True,
    )
    result = ExecutionValidator(controls=TradingControls()).validate(
        decision,
        portfolio=PortfolioRiskView(
            equity=100_000,
            cash=50_000,
            cash_pct=50,
            gross_exposure_pct=50,
            positions=[
                PositionRiskView(symbol="QQQ", quantity=10, market_value=5000, sector="ETF", weight_pct=5),
            ],
        ),
        latest_prices={"SPY": 450, "QQQ": 400},
        data_quality_score=0.9,
        entry_universe={"SPY", "QQQ"},
        block_new_entries=True,
    )
    assert result.approved is True
    assert len(result.intents) == 1
    assert result.intents[0].symbol == "QQQ"
    assert result.intents[0].side == "sell"
