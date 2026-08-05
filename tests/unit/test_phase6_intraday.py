"""Phase 6 — intraday event bus, monitor, exits, closing, settlement, E2E mock."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.brokers.base import OrderRequest, OrderSide
from app.brokers.mock import MockBroker
from app.core.config import Settings
from app.core.database import Base
from app.execution.safety_controls import TradingControls
from app.execution.service import ExecutionService
from app.intraday.events import IntradayEventBus
from app.intraday.exits import ExitPolicyEngine, StopKind
from app.intraday.modes import IntradayOperationMode, ModeCapabilities, resolve_mode
from app.intraday.monitor import EXIT_INTENT_REQUIRED, PositionMonitor
from app.intraday.pnl import apply_fill_fifo
from app.intraday.risk import DynamicRiskRevalidator
from app.intraday.service import IntradayService
from app.models import PositionLifecycle
from app.risk import PortfolioRiskView
from app.schemas.cio import CIODecision, SymbolActionPlan
from app.schemas.common import MarketRegime, OrderType, PortfolioAction, SymbolAction


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


def _settings(**kwargs: object) -> Settings:
    base = dict(
        app_env="test",
        broker_provider="mock",
        broker_environment="paper",
        enable_broker_orders=False,
        require_manual_order_approval=True,
        enable_live_trading=False,
        intraday_operation_mode="OBSERVE_ONLY",
        enable_intraday_monitoring=True,
        enable_intraday_agent_reanalysis=True,
        max_intraday_reanalyses=12,
        event_deduplication_window_seconds=300,
        auto_execute_hard_stops=False,
        allow_stop_widening=False,
        allow_stop_tightening=True,
        default_closing_policy="CLOSE_INTRADAY_ONLY",
        starting_cash=25_000.0,
    )
    base.update(kwargs)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_event_bus_dedup_and_priority(session: AsyncSession) -> None:
    bus = IntradayEventBus(session, settings=_settings())
    a = await bus.publish(
        event_type="HIGH_IMPORTANCE_NEWS",
        source="test",
        symbols=["NVDA"],
        deduplication_key="news:1",
        requires_analysis=True,
    )
    b = await bus.publish(
        event_type="HIGH_IMPORTANCE_NEWS",
        source="test",
        symbols=["NVDA"],
        deduplication_key="news:1",
        requires_analysis=True,
    )
    assert a.status == "NEW"
    assert b.status == "DEDUPLICATED"
    assert a.priority >= 60
    # Original row stays NEW so pending drains still see it.
    from app.models import IntradayEvent
    from sqlalchemy import select

    row = (await session.execute(select(IntradayEvent))).scalar_one()
    assert row.status == "NEW"
    assert int(row.revision or 1) >= 2


def test_reanalysis_cooldown() -> None:
    bus = IntradayEventBus.__new__(IntradayEventBus)
    bus.settings = _settings(min_global_reanalysis_gap_minutes=10, max_intraday_reanalyses=2)
    bus._reanalysis_times = [datetime.now(UTC)]
    bus._symbol_reanalysis = {}
    ok, why = bus.allow_reanalysis(symbols=["SPY"])
    assert not ok and why == "global_cooldown"
    ok2, _ = bus.allow_reanalysis(symbols=["SPY"], bypass=True)
    assert ok2


def test_modes_observe_blocks_submit() -> None:
    caps = ModeCapabilities(IntradayOperationMode.OBSERVE_ONLY)
    assert caps.can_analyze
    assert not caps.can_submit
    assert ModeCapabilities(IntradayOperationMode.MANUAL_APPROVAL).can_submit
    assert resolve_mode(_settings(), emergency=True) == IntradayOperationMode.EMERGENCY_STOP


@pytest.mark.asyncio
async def test_position_monitor_stop_trigger(session: AsyncSession) -> None:
    settings = _settings()
    mon = PositionMonitor(session, settings=settings)
    lc = await mon.ensure_lifecycle_from_broker(
        symbol="NVDA", quantity=10, avg_entry=100, stop_price=95
    )
    result = await mon.evaluate(lc, current_price=94.0, equity=25_000)
    assert result.verdict == EXIT_INTENT_REQUIRED
    assert "stop_triggered" in result.reasons
    snaps = await IntradayService(session, settings=settings).snapshots(lc.id)
    assert snaps


@pytest.mark.asyncio
async def test_stop_widening_blocked(session: AsyncSession) -> None:
    eng = ExitPolicyEngine(session, settings=_settings(allow_stop_widening=False, allow_stop_tightening=True))
    assert eng.adjust_stop(current_stop=95.0, proposed_stop=90.0) == 95.0
    assert eng.adjust_stop(current_stop=95.0, proposed_stop=97.0) == 97.0


@pytest.mark.asyncio
async def test_take_profit_partial(session: AsyncSession) -> None:
    eng = ExitPolicyEngine(session, settings=_settings())
    lc = PositionLifecycle(
        id=uuid4(),
        symbol="SPY",
        status="OPEN",
        quantity=100,
        average_entry_price=100,
        current_price=110,
        take_profit_targets=[{"price": 108, "fraction": 0.25}, {"price": 112, "fraction": 0.25}],
        filled_take_profit_indices=[],
        exit_policy={},
    )
    session.add(lc)
    await session.flush()
    r = await eng.check_take_profit(lc, price=109)
    assert r.triggered and r.quantity_to_exit == 25.0
    r2 = await eng.check_take_profit(lc, price=109)
    assert not r2.triggered  # same target already filled


@pytest.mark.asyncio
async def test_dynamic_risk_exit(session: AsyncSession) -> None:
    lc = PositionLifecycle(
        id=uuid4(), symbol="QQQ", status="OPEN", quantity=50, average_entry_price=100, stop_price=90, exit_policy={}
    )
    session.add(lc)
    await session.flush()
    r = await DynamicRiskRevalidator(session, settings=_settings()).evaluate(
        lc, equity=25_000, daily_pnl_pct=-2.0, drawdown_pct=1.0, price=100
    )
    assert r.status == "EMERGENCY_STOP_REQUIRED"


def test_fifo_pnl() -> None:
    from app.intraday.pnl import Lot

    lots = [Lot(10, 100, datetime.now(UTC))]
    r = apply_fill_fifo(lots, side="sell", quantity=4, price=110, equity=25_000)
    assert r.gross_realized_pl == 40.0
    assert abs(sum(l.quantity for l in r.remaining_lots) - 6) < 1e-9


@pytest.mark.asyncio
async def test_closing_and_overnight(session: AsyncSession) -> None:
    settings = _settings(intraday_operation_mode="MANUAL_APPROVAL")
    svc = IntradayService(session, settings=settings)
    await svc.monitor.ensure_lifecycle_from_broker(symbol="AAPL", quantity=5, avg_entry=150, stop_price=140)
    lc = (await svc.monitor.list_lifecycles())[0]
    lc.overnight_allowed = False
    closing = await svc.closing.run_closing()
    assert closing["broker_orders_submitted"] is False
    assert any(p["action"] == "close" for p in closing["plans"])
    overnight = await svc.closing.overnight_review()
    assert overnight["reviews"][0]["status"] == "CLOSE_BEFORE_MARKET_CLOSE"


@pytest.mark.asyncio
async def test_reduce_close_via_intent_path(session: AsyncSession) -> None:
    settings = _settings(intraday_operation_mode="MANUAL_APPROVAL")
    svc = IntradayService(session, settings=settings)
    lc = await svc.monitor.ensure_lifecycle_from_broker(symbol="MSFT", quantity=8, avg_entry=300, stop_price=280)
    result = await svc.close_position(lc.id)
    assert result["broker_orders_submitted"] is False
    assert result["path"] == "intent_risk_approval_execution"
    assert result["intent_id"]


@pytest.mark.asyncio
async def test_settlement_and_posttrade(session: AsyncSession) -> None:
    settings = _settings()
    svc = IntradayService(session, settings=settings)
    settle = await svc.settlement.settle()
    assert "settlement_id" in settle
    assert settle["broker_orders_submitted"] is False
    lc = await svc.monitor.ensure_lifecycle_from_broker(symbol="AMD", quantity=2, avg_entry=100)
    review = await svc.posttrade.create_review(
        position_lifecycle_id=lc.id,
        symbol="AMD",
        outcome="closed",
        exit_reason="test",
        agent_runs=[{"agent_name": "cio", "directional_view": "bullish", "confidence": 0.7}],
    )
    assert review["strategy_auto_changed"] is False
    assert review["agent_assessment_ids"]


@pytest.mark.asyncio
async def test_recovery_blocks_on_emergency(session: AsyncSession) -> None:
    controls = TradingControls()
    controls.emergency_stop("test")
    from app.execution.ops_persistence import persist_trading_controls

    await persist_trading_controls(session, controls)
    result = await IntradayService(session, settings=_settings(), controls=controls).recovery.run()
    assert result["emergency_stop"] is True
    assert result["new_orders_allowed"] is False


@pytest.mark.asyncio
async def test_mock_intraday_e2e_simulation(session: AsyncSession) -> None:
    """Premarket intent → approve path → fill → monitor → reduce draft → closing → settle → review."""
    settings = _settings(
        enable_broker_orders=True,
        enable_broker_connection=True,
        require_manual_order_approval=True,
        intraday_operation_mode="MANUAL_APPROVAL",
    )
    controls = TradingControls()
    broker = MockBroker(seed=6, starting_cash=25_000)
    broker.prices["SPY"] = 100.0

    # Entry via execution service
    exec_svc = ExecutionService(session, settings=settings, controls=controls)
    exec_svc._broker = broker
    decision = CIODecision(
        timestamp=datetime.now(UTC),
        market_regime=MarketRegime.NEUTRAL,
        portfolio_action=PortfolioAction.BUY,
        symbol_actions=[
            SymbolActionPlan(
                symbol="SPY",
                action=SymbolAction.BUY,
                confidence=70,
                target_position_pct=5,
                order_type=OrderType.LIMIT,
                stop_loss=95.0,
                thesis="e2e",
                invalidation="break 95",
            )
        ],
        cash_target_pct=70,
        risk_approval=True,
    )
    intents = await exec_svc.build_intents_from_decision(
        decision,
        portfolio=PortfolioRiskView(equity=25_000, cash=25_000, cash_pct=100, gross_exposure_pct=0),
        latest_prices={"SPY": 100.0},
    )
    assert intents
    intent = intents[0]
    await exec_svc.validate_intent(
        intent.id,
        equity=25_000,
        cash=25_000,
        buying_power=25_000,
        gross_exposure=0,
        position_qty=0,
    )
    await exec_svc.approve_intent(intent.id)
    order = await exec_svc.submit_intent(intent.id)
    assert order is not None and order.broker_order_id

    intra = IntradayService(session, settings=settings, controls=controls)
    lc = await intra.monitor.ensure_lifecycle_from_broker(
        symbol="SPY",
        quantity=float(order.qty),
        avg_entry=100.0,
        decision_id=intent.decision_id,
        stop_price=95.0,
    )
    lc.take_profit_targets = [{"price": 110, "fraction": 0.5}]
    # Market event + monitor
    await intra.bus.publish(
        event_type="VOLATILITY_SPIKE",
        source="sim",
        symbols=["SPY"],
        deduplication_key="vol:spy:1",
        requires_analysis=True,
        importance="high",
    )
    mon = await intra.monitor_all(prices={"SPY": 101.0})
    assert mon
    # Reduce intent (not auto broker)
    red = await intra.reduce_position(lc.id, fraction=0.5)
    assert red["broker_orders_submitted"] is False
    closing = await intra.closing.run_closing()
    assert closing["broker_orders_submitted"] is False
    settle = await intra.settlement.settle()
    assert settle["settlement_id"]
    review = await intra.posttrade.create_review(
        position_lifecycle_id=lc.id, symbol="SPY", outcome="closed", exit_reason="e2e", pnl=10.0
    )
    assert review["review_id"]


@pytest.mark.skipif(
    __import__("os").environ.get("RUN_ALPACA_PAPER_INTRADAY_SMOKE_TESTS") != "true",
    reason="opt-in Alpaca paper intraday smoke only",
)
@pytest.mark.asyncio
async def test_alpaca_paper_intraday_smoke_opt_in() -> None:
    pytest.skip("credential-gated; not run in default suite")
