"""Phase 5 — Mock broker, sizing, pretrade, state machine, execution E2E."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.brokers.mock import MockBroker
from app.brokers.base import OrderRequest, OrderSide, OrderStatus
from app.brokers.errors import BrokerError
from app.brokers.factory import get_broker
from app.brokers.models import (
    BrokerOrderRequest,
    InternalOrderState,
    PretradeStatus,
    assert_order_transition,
    redact_account_id,
)
from app.core.config import Settings, clear_settings_cache
from app.core.database import Base
from app.execution.policy import ExecutionPolicyInput, select_execution
from app.execution.pretrade import PretradeRiskValidator
from app.execution.safety_controls import TradingControls
from app.execution.service import ExecutionService, make_client_order_id
from app.execution.sizing import SizingInput, size_position
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
        enable_broker_connection=True,
        enable_broker_orders=True,
        enable_automated_execution=False,
        enable_external_data=False,
        enable_market_data_collection=False,
        require_manual_order_approval=True,
        enable_live_trading=False,
        enable_short_selling=False,
        starting_cash=25_000.0,
    )
    base.update(kwargs)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_mock_broker_submit_fill_cancel_replace() -> None:
    b = MockBroker(seed=7, starting_cash=10_000)
    b.prices["SPY"] = 100.0
    order = await b.submit_order(
        OrderRequest(
            symbol="SPY",
            side=OrderSide.BUY,
            qty=2,
            order_type="limit",
            limit_price=100.0,
            idempotency_key="c1",
        )
    )
    assert order.status.value in {"filled", "partially_filled"}
    acct = await b.get_account_canonical()
    assert acct.account_id_reference.startswith("acct_")
    assert "mock-7" not in acct.account_id_reference or "***" in acct.account_id_reference

    b.partial_fill_fraction = 0.5
    partial = await b.submit_order(
        OrderRequest(
            symbol="QQQ",
            side=OrderSide.BUY,
            qty=4,
            order_type="limit",
            limit_price=50.0,
            idempotency_key="c2",
        )
    )
    assert partial.filled_qty == 2.0

    dup = await b.submit_order(
        OrderRequest(
            symbol="SPY",
            side=OrderSide.BUY,
            qty=1,
            order_type="market",
            idempotency_key="c1",
        )
    )
    assert dup.broker_order_id == order.broker_order_id

    b.timeout_next = True
    with pytest.raises(TimeoutError):
        await b.submit_order(
            OrderRequest(symbol="IWM", side=OrderSide.BUY, qty=1, order_type="market")
        )

    b.fail_next = True
    with pytest.raises(BrokerError):
        await b.submit_order(
            OrderRequest(symbol="IWM", side=OrderSide.BUY, qty=1, order_type="market")
        )

    # leave an open-ish order then cancel
    b.partial_fill_fraction = None
    open_o = await b.submit_order(
        OrderRequest(
            symbol="DIA",
            side=OrderSide.BUY,
            qty=1,
            order_type="limit",
            limit_price=100.0,
            idempotency_key="open1",
        )
    )
    # force status accepted for cancel test by mutating
    b.orders[open_o.broker_order_id] = open_o.__class__(
        broker_order_id=open_o.broker_order_id,
        status=OrderStatus.ACCEPTED,
        submitted_at=open_o.submitted_at,
        filled_qty=0,
        avg_fill_price=None,
        raw=open_o.raw,
    )
    canceled = await b.cancel_order(open_o.broker_order_id)
    assert canceled.status == OrderStatus.CANCELED

    replaced = await b.replace_order(
        open_o.broker_order_id,
        OrderRequest(
            symbol="DIA",
            side=OrderSide.BUY,
            qty=1,
            order_type="limit",
            limit_price=99.0,
            idempotency_key="repl1",
        ),
    )
    assert replaced.broker_order_id != open_o.broker_order_id


def test_sizing_caps_and_invalid_stop() -> None:
    ok = size_position(
        SizingInput(
            portfolio_equity=25_000,
            risk_per_trade_pct=0.5,
            entry_price=100,
            stop_price=95,
            max_position_pct=10,
            available_buying_power=20_000,
            min_cash_pct=30,
        )
    )
    assert ok.approved and ok.quantity > 0
    bad = size_position(
        SizingInput(
            portfolio_equity=25_000,
            risk_per_trade_pct=0.5,
            entry_price=100,
            stop_price=100,
            max_position_pct=10,
            available_buying_power=20_000,
            min_cash_pct=30,
        )
    )
    assert not bad.approved
    reverse = size_position(
        SizingInput(
            portfolio_equity=25_000,
            risk_per_trade_pct=0.5,
            entry_price=100,
            stop_price=105,
            max_position_pct=10,
            available_buying_power=20_000,
            min_cash_pct=30,
            side="buy",
        )
    )
    assert not reverse.approved


def test_pretrade_blocks_and_manual_approval() -> None:
    controls = TradingControls()
    v = PretradeRiskValidator(settings=_settings(), controls=controls)
    approved = v.validate(
        intent_id=str(uuid4()),
        decision_id=str(uuid4()),
        symbol="SPY",
        side="buy",
        quantity=10,
        entry_price=100,
        stop_price=95,
        equity=25_000,
        cash=25_000,
        buying_power=25_000,
        gross_exposure=0,
        position_qty=0,
        data_quality_score=1.0,
        quote_age_seconds=1,
        spread_bps=10,
    )
    assert approved.status == PretradeStatus.REQUIRES_MANUAL_APPROVAL

    controls.emergency_stop("test")
    blocked = v.validate(
        intent_id=str(uuid4()),
        decision_id=None,
        symbol="SPY",
        side="buy",
        quantity=1,
        entry_price=100,
        stop_price=95,
        equity=25_000,
        cash=25_000,
        buying_power=25_000,
        gross_exposure=0,
        position_qty=0,
        data_quality_score=1.0,
        quote_age_seconds=1,
        spread_bps=10,
    )
    assert blocked.status == PretradeStatus.SYSTEM_BLOCKED
    controls.clear_emergency()
    controls.resume()


def test_order_state_machine_guards() -> None:
    assert_order_transition(InternalOrderState.APPROVED, InternalOrderState.SUBMITTING)
    with pytest.raises(ValueError):
        assert_order_transition(InternalOrderState.FILLED, InternalOrderState.SUBMITTING)


def test_broker_order_request_qty_xor_notional() -> None:
    with pytest.raises(ValueError):
        BrokerOrderRequest(client_order_id="x", symbol="SPY", side="buy", quantity=1, notional=100)
    BrokerOrderRequest(client_order_id="x", symbol="SPY", side="buy", quantity=1)


def test_execution_policy_blocks_stale_and_wide_spread() -> None:
    stale = select_execution(
        ExecutionPolicyInput(
            symbol="SPY",
            side="buy",
            quantity=1,
            entry_price=100,
            stop_price=95,
            spread_bps=10,
            quote_age_seconds=9999,
            liquidity_shares=1_000_000,
            market_open=True,
            minutes_to_close=120,
        )
    )
    assert not stale.allowed
    wide = select_execution(
        ExecutionPolicyInput(
            symbol="SPY",
            side="buy",
            quantity=1,
            entry_price=100,
            stop_price=95,
            spread_bps=80,
            quote_age_seconds=1,
            liquidity_shares=1_000_000,
            market_open=True,
            minutes_to_close=120,
            preferred_order_type="market",
            max_spread_bps=50,
        )
    )
    assert wide.allowed and wide.order_type == "limit"


def test_live_factory_blocked() -> None:
    clear_settings_cache()
    with pytest.raises(BrokerError):
        get_broker(_settings(enable_live_trading=True))
    with pytest.raises(BrokerError):
        get_broker(_settings(broker_environment="live"))


def test_ibkr_live_port_gate() -> None:
    from app.brokers.ibkr import IbkrBroker

    s = _settings(
        broker_provider="ibkr",
        enable_broker_connection=True,
        broker_environment="paper",
        ibkr_port=4001,  # live Gateway port
        ibkr_allow_live_ports=False,
    )
    with pytest.raises(BrokerError, match="ibkr_port_looks_live"):
        IbkrBroker(s)


def test_redact_account_id() -> None:
    assert redact_account_id("ABCDEFGH").endswith("EFGH")
    assert "***" in redact_account_id("ABCDEFGH")


@pytest.mark.asyncio
async def test_execution_e2e_intent_approve_submit(session: AsyncSession) -> None:
    settings = _settings(require_manual_order_approval=True)
    controls = TradingControls()
    svc = ExecutionService(session, settings=settings, controls=controls)
    svc._broker = MockBroker(seed=99, starting_cash=25_000)
    svc._broker.prices["SPY"] = 100.0

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
                thesis="unit e2e",
                invalidation="break 95",
            )
        ],
        cash_target_pct=70,
        risk_approval=True,
    )
    # Provide entry via validator prices
    portfolio = PortfolioRiskView(equity=25_000, cash=25_000, cash_pct=100, gross_exposure_pct=0)
    intents = await svc.build_intents_from_decision(
        decision, portfolio=portfolio, latest_prices={"SPY": 100.0}
    )
    assert intents
    intent = intents[0]
    result = await svc.validate_intent(
        intent.id,
        equity=25_000,
        cash=25_000,
        buying_power=25_000,
        gross_exposure=0,
        position_qty=0,
    )
    assert result.status == PretradeStatus.REQUIRES_MANUAL_APPROVAL
    assert intent.status == "PENDING_APPROVAL"

    with pytest.raises(Exception):
        await svc.submit_intent(intent.id)

    await svc.approve_intent(intent.id)
    order = await svc.submit_intent(intent.id)
    assert order is not None
    assert order.idempotency_key
    # idempotent resubmit
    again = await svc.submit_intent(intent.id)
    assert again is not None
    assert again.id == order.id


@pytest.mark.asyncio
async def test_emergency_stop_blocks_submit(session: AsyncSession) -> None:
    settings = _settings(require_manual_order_approval=False)
    controls = TradingControls()
    svc = ExecutionService(session, settings=settings, controls=controls)
    svc._broker = MockBroker(seed=1)
    from app.models import OrderIntent

    intent = OrderIntent(
        id=uuid4(),
        symbol="SPY",
        intent_type="OPEN_LONG",
        side="buy",
        quantity=1,
        approved_quantity=1,
        entry_price=100,
        stop_price=95,
        status="APPROVED",
        exit_policy={},
        metadata_json={"order_type": "limit"},
    )
    session.add(intent)
    await session.flush()
    controls.emergency_stop("e2e")
    with pytest.raises(BrokerError):
        await svc.submit_intent(intent.id)


def test_client_order_id_stable() -> None:
    a = make_client_order_id(
        workflow_run_id="w",
        decision_id="d",
        intent_id="i",
        symbol="SPY",
        side="buy",
        attempt=1,
    )
    b = make_client_order_id(
        workflow_run_id="w",
        decision_id="d",
        intent_id="i",
        symbol="SPY",
        side="buy",
        attempt=1,
    )
    assert a == b and a.startswith("inv-")


@pytest.mark.asyncio
async def test_approval_expiry(session: AsyncSession) -> None:
    settings = _settings(order_approval_expiry_minutes=0)
    # Force expiry by setting expires_at in the past after validate
    svc = ExecutionService(session, settings=_settings(), controls=TradingControls())
    from app.models import OrderApproval, OrderIntent

    intent = OrderIntent(
        id=uuid4(),
        symbol="SPY",
        intent_type="OPEN_LONG",
        side="buy",
        quantity=1,
        entry_price=100,
        stop_price=95,
        status="PENDING_APPROVAL",
        exit_policy={},
        metadata_json={},
    )
    session.add(intent)
    session.add(
        OrderApproval(
            id=uuid4(),
            intent_id=intent.id,
            status="PENDING_APPROVAL",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    await session.flush()
    with pytest.raises(ValueError, match="approval_expired"):
        await svc.approve_intent(intent.id)
