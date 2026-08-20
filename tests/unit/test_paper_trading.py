"""Phase 6 paper trading tests (simulated broker — no network)."""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.brokers.mock import MockBroker as SimulatedBroker
from app.core.config import Settings
from app.core.database import Base
from app.execution.order_manager import OrderManager
from app.execution.position_manager import PositionManager
from app.execution.safety_controls import TradingControls
from app.execution.validation import ExecutionValidationResult, ValidatedOrderIntent


def _exec_settings() -> Settings:
    return Settings(
        app_env="test",
        enable_broker_orders=True,
        enable_automated_execution=True,
        require_manual_order_approval=False,
        enable_live_trading=False,
        broker_provider="mock",
        broker_environment="paper",
    )


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
async def test_simulated_submit_and_sync(session: AsyncSession) -> None:
    broker = SimulatedBroker()
    controls = TradingControls()
    om = OrderManager(session, broker=broker, controls=controls, settings=_exec_settings())
    validation = ExecutionValidationResult(
        approved=True,
        intents=[
            ValidatedOrderIntent(
                symbol="QQQ",
                side="buy",
                quantity=2,
                order_type="limit",
                limit_price=100.0,
                stop_price=95.0,
                idempotency_key=f"test-{uuid4()}",
                decision_id=str(uuid4()),
                thesis="unit",
            )
        ],
    )
    orders = await om.submit_validated_intents(validation)
    assert len(orders) == 1
    assert orders[0].status.upper() == "FILLED"
    assert orders[0].broker_order_id

    pm = PositionManager(session, broker=broker)
    synced = await pm.sync_from_broker()
    assert synced["open_positions"] == 1
    state = await pm.portfolio_state_input()
    assert state.positions[0].symbol == "QQQ"


@pytest.mark.asyncio
async def test_idempotent_order_not_duplicated(session: AsyncSession) -> None:
    broker = SimulatedBroker()
    om = OrderManager(session, broker=broker, controls=TradingControls(), settings=_exec_settings())
    key = "idem-key-1"
    intent = ValidatedOrderIntent(
        symbol="SPY",
        side="buy",
        quantity=1,
        order_type="market",
        limit_price=None,
        stop_price=400.0,
        idempotency_key=key,
        decision_id=str(uuid4()),
        thesis="dup",
    )
    v = ExecutionValidationResult(approved=True, intents=[intent])
    first = await om.submit_validated_intents(v)
    second = await om.submit_validated_intents(v)
    assert len(first) == 1
    assert second == []


@pytest.mark.asyncio
async def test_skips_partial_exit_when_working_sell_exists(session: AsyncSession) -> None:
    from app.models import Order

    broker = SimulatedBroker()
    om = OrderManager(session, broker=broker, controls=TradingControls(), settings=_exec_settings())
    session.add(
        Order(
            idempotency_key="working-vas-flatten",
            symbol="VAS",
            side="sell",
            qty=876,
            order_type="market",
            status="ACCEPTED",
            decision_id=uuid4(),
        )
    )
    await session.flush()
    v = ExecutionValidationResult(
        approved=True,
        intents=[
            ValidatedOrderIntent(
                symbol="VAS",
                side="sell",
                quantity=438,
                order_type="market",
                limit_price=None,
                stop_price=None,
                idempotency_key=f"{uuid4()}:VAS:sell:PARTIAL_SELL",
                decision_id=str(uuid4()),
                thesis="cio partial",
                venue="AU",
            )
        ],
    )
    assert await om.submit_validated_intents(v) == []


@pytest.mark.asyncio
async def test_skips_flatten_when_same_key_is_working(session: AsyncSession) -> None:
    from sqlalchemy import select

    from app.models import Order

    broker = SimulatedBroker()
    om = OrderManager(session, broker=broker, controls=TradingControls(), settings=_exec_settings())
    key = "hard-stop:vas-lc:2026-08-20"
    session.add(
        Order(
            idempotency_key=key,
            symbol="VAS",
            side="sell",
            qty=876,
            order_type="limit",
            limit_price=110.0,
            status="ACCEPTED",
            decision_id=uuid4(),
        )
    )
    await session.flush()
    v = ExecutionValidationResult(
        approved=True,
        intents=[
            ValidatedOrderIntent(
                symbol="VAS",
                side="sell",
                quantity=876,
                order_type="market",
                limit_price=None,
                stop_price=None,
                idempotency_key=key,
                decision_id=str(uuid4()),
                thesis="hard_stop",
                venue="AU",
            )
        ],
    )
    assert await om.submit_validated_intents(v) == []
    row = (await session.execute(select(Order).where(Order.idempotency_key == key))).scalar_one()
    assert row.status == "ACCEPTED"


@pytest.mark.asyncio
async def test_reuses_cancelled_flatten_key(session: AsyncSession) -> None:
    from app.models import Order

    broker = SimulatedBroker()
    broker.positions["VAS"] = {"symbol": "VAS", "qty": "876", "avg_entry_price": "114"}
    om = OrderManager(session, broker=broker, controls=TradingControls(), settings=_exec_settings())
    key = "force-close:vas-lc:close:2026-08-20"
    session.add(
        Order(
            idempotency_key=key,
            symbol="VAS",
            side="sell",
            qty=876,
            order_type="market",
            status="CANCELLED",
            decision_id=uuid4(),
        )
    )
    await session.flush()
    v = ExecutionValidationResult(
        approved=True,
        intents=[
            ValidatedOrderIntent(
                symbol="VAS",
                side="sell",
                quantity=876,
                order_type="market",
                limit_price=None,
                stop_price=112.0,
                idempotency_key=key,
                decision_id=str(uuid4()),
                thesis="force_close:leftover",
                venue="AU",
            )
        ],
    )
    rows = await om.submit_validated_intents(v)
    assert len(rows) == 1
    assert rows[0].idempotency_key == key
    assert rows[0].status.upper() == "FILLED"


@pytest.mark.asyncio
async def test_controls_block_submit(session: AsyncSession) -> None:
    broker = SimulatedBroker()
    controls = TradingControls()
    controls.pause()
    om = OrderManager(session, broker=broker, controls=controls, settings=_exec_settings())
    v = ExecutionValidationResult(
        approved=True,
        intents=[
            ValidatedOrderIntent(
                symbol="QQQ",
                side="buy",
                quantity=1,
                order_type="market",
                limit_price=None,
                stop_price=90.0,
                idempotency_key=str(uuid4()),
                decision_id=str(uuid4()),
                thesis="blocked",
            )
        ],
    )
    assert await om.submit_validated_intents(v) == []


@pytest.mark.asyncio
async def test_broker_error_fail_closed(session: AsyncSession) -> None:
    broker = SimulatedBroker()
    broker.fail_next = True
    om = OrderManager(session, broker=broker, controls=TradingControls(), settings=_exec_settings())
    v = ExecutionValidationResult(
        approved=True,
        intents=[
            ValidatedOrderIntent(
                symbol="QQQ",
                side="buy",
                quantity=1,
                order_type="market",
                limit_price=None,
                stop_price=90.0,
                idempotency_key=str(uuid4()),
                decision_id=str(uuid4()),
                thesis="fail",
            )
        ],
    )
    rows = await om.submit_validated_intents(v)
    assert len(rows) == 1
    assert rows[0].status.upper() == "REJECTED"


@pytest.mark.asyncio
async def test_validation_reject_creates_no_orders(session: AsyncSession) -> None:
    om = OrderManager(
        session, broker=SimulatedBroker(), controls=TradingControls(), settings=_exec_settings()
    )
    v = ExecutionValidationResult(approved=False, rejections=["hard_veto"])
    assert await om.submit_validated_intents(v) == []
