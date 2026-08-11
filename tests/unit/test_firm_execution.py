"""Agent-firm execution bridge tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, TradingMode, clear_settings_cache
from app.core.database import Base
from app.execution.firm_execution import materialize_cio_decision, paper_auto_submit_allowed
from app.schemas.cio import CIODecision
from app.schemas.common import MarketRegime, PortfolioAction
from app.schemas.risk_manager import PortfolioStateInput


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


def test_paper_auto_submit_requires_explicit_unlock() -> None:
    clear_settings_cache()
    locked = Settings(
        enable_broker_orders=False,
        enable_automated_execution=False,
        require_manual_order_approval=False,
        enable_live_trading=False,
        trading_mode=TradingMode.PAPER,
    )
    assert paper_auto_submit_allowed(locked) is False

    armed = Settings(
        enable_broker_orders=True,
        enable_automated_execution=True,
        require_manual_order_approval=False,
        enable_live_trading=False,
        trading_mode=TradingMode.PAPER,
        broker_environment="paper",
    )
    assert paper_auto_submit_allowed(armed) is True

    brake = Settings(
        enable_broker_orders=True,
        enable_automated_execution=True,
        require_manual_order_approval=True,
        enable_live_trading=False,
        trading_mode=TradingMode.PAPER,
        broker_environment="paper",
    )
    assert paper_auto_submit_allowed(brake) is False

    live_blocked = Settings(
        enable_broker_orders=True,
        enable_automated_execution=True,
        require_manual_order_approval=False,
        enable_live_trading=True,
        trading_mode=TradingMode.PAPER,
        broker_environment="paper",
    )
    assert paper_auto_submit_allowed(live_blocked) is False


@pytest.mark.asyncio
async def test_materialize_no_trade_creates_no_intents(session: AsyncSession) -> None:
    clear_settings_cache()
    settings = Settings(
        enable_broker_orders=False,
        enable_automated_execution=False,
        require_manual_order_approval=False,
    )
    cio = CIODecision(
        decision_id=uuid4(),
        timestamp=datetime.now(UTC),
        market_regime=MarketRegime.NEUTRAL,
        portfolio_action=PortfolioAction.NO_TRADE,
        symbol_actions=[],
        cash_target_pct=100.0,
        risk_approval=True,
        hard_veto_honored=True,
        reason_not_to_trade="flat",
    )
    portfolio = PortfolioStateInput(
        as_of=datetime.now(UTC),
        equity=25_000.0,
        cash=25_000.0,
        cash_pct=100.0,
        gross_exposure_pct=0.0,
    )
    result = await materialize_cio_decision(
        session,
        cio,
        portfolio=portfolio,
        latest_prices={},
        settings=settings,
    )
    assert result["actor"] == "cio_bottom_up"
    assert result["intent_count"] == 0
    assert result["broker_orders_submitted"] is False
    assert result["validation_approved"] is True
    assert "no_symbols_to_materialize" in result["notes"]
    assert result["live_trading_blocked"] is False


@pytest.mark.asyncio
async def test_materialize_no_trade_skips_live_price_gate(session: AsyncSession) -> None:
    clear_settings_cache()
    settings = Settings(
        enable_broker_orders=True,
        enable_automated_execution=True,
        enable_external_data=True,
        broker_provider="ibkr",
        trading_mode=TradingMode.PAPER,
    )
    cio = CIODecision(
        decision_id=uuid4(),
        timestamp=datetime.now(UTC),
        market_regime=MarketRegime.NEUTRAL,
        portfolio_action=PortfolioAction.NO_TRADE,
        symbol_actions=[],
        cash_target_pct=100.0,
        risk_approval=True,
        hard_veto_honored=True,
        reason_not_to_trade="flat",
    )
    portfolio = PortfolioStateInput(
        as_of=datetime.now(UTC),
        equity=25_000.0,
        cash=25_000.0,
        cash_pct=100.0,
        gross_exposure_pct=0.0,
    )
    result = await materialize_cio_decision(
        session,
        cio,
        portfolio=portfolio,
        latest_prices={"AAPL": 220.0},
        settings=settings,
    )
    assert result["intent_count"] == 0
    assert result["validation_approved"] is True
    assert "live_prices_unavailable" not in ",".join(result["notes"])
