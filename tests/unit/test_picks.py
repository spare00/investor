"""Compact pick ledger — selected vs rejected names."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agents.pipeline import AnalysisBundle
from app.brokers.models import IntentStatus
from app.core.database import Base
from app.models import CIODecisionRecord, OrderIntent
from app.schemas import (
    CIODecision,
    DevilsAdvocateOutput,
    MacroStrategistOutput,
    MarketIntelligenceOutput,
    MarketRegime,
    PortfolioAction,
    QuantStrategistOutput,
    RiskManagerOutput,
    RiskVerdict,
)
from app.schemas.cio import SymbolActionPlan
from app.schemas.common import (
    BreadthState,
    LiquidityState,
    MomentumState,
    PriceZone,
    SymbolAction,
    TrendState,
    VolatilityState,
)
from app.schemas.quant_strategist import SymbolQuantView
from app.services.audit import AuditService
from app.services.picks import PicksService, proposed_from_quant, rows_for_decision

NOW = datetime(2026, 8, 6, 14, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


def _view(symbol: str, *, probability: float = 0.62) -> SymbolQuantView:
    return SymbolQuantView(
        symbol=symbol,
        trend_state=TrendState.UP,
        momentum_state=MomentumState.STEADY,
        volatility_state=VolatilityState.NORMAL,
        liquidity_state=LiquidityState.NORMAL,
        entry_zone=PriceZone(min=100, max=101),
        stop_or_invalidation=98,
        probability_estimate=probability,
        probability_basis="tape",
        notes=[f"{symbol} setup"],
    )


def _analysis(
    *,
    symbol_actions: list[SymbolActionPlan] | None = None,
    portfolio_action: PortfolioAction = PortfolioAction.SCALE_IN,
    reason_not_to_trade: str | None = None,
    prefer_no_trade: bool = False,
    views: list[SymbolQuantView] | None = None,
) -> AnalysisBundle:
    return AnalysisBundle(
        workflow_id=uuid4(),
        market_intelligence=MarketIntelligenceOutput(
            timestamp=NOW, data_quality_score=0.8, market_events=[], top_market_themes=[]
        ),
        macro=MacroStrategistOutput(
            timestamp=NOW,
            market_regime=MarketRegime.RISK_ON,
            confidence=0.7,
            data_quality_score=0.8,
        ),
        quant=QuantStrategistOutput(
            timestamp=NOW,
            market_trend_state=TrendState.UP,
            market_momentum_state=MomentumState.STEADY,
            market_volatility_state=VolatilityState.NORMAL,
            market_breadth_state=BreadthState.HEALTHY,
            market_liquidity_state=LiquidityState.NORMAL,
            data_quality_score=0.8,
            symbol_views=list(views or []),
        ),
        risk=RiskManagerOutput(
            timestamp=NOW,
            overall_verdict=RiskVerdict.APPROVED,
            cash_pct=80,
            gross_exposure_pct=20,
        ),
        devil=DevilsAdvocateOutput(
            timestamp=NOW,
            strongest_reason_thesis_is_wrong="fade",
            information_already_in_price=False,
            information_already_in_price_rationale="n/a",
            opposing_market_scenario="fade",
            prefer_no_trade=prefer_no_trade,
            prefer_no_trade_rationale="wait for confirmation",
            challenge_score=0.4,
        ),
        cio=CIODecision(
            timestamp=NOW,
            market_regime=MarketRegime.RISK_ON,
            portfolio_action=portfolio_action,
            symbol_actions=list(symbol_actions or []),
            cash_target_pct=70,
            risk_approval=True,
            reason_not_to_trade=reason_not_to_trade,
        ),
        completed_at=NOW,
    )


def test_proposed_from_quant_uses_playbook_gate() -> None:
    payload = QuantStrategistOutput(
        timestamp=NOW,
        market_trend_state=TrendState.UP,
        market_momentum_state=MomentumState.STEADY,
        market_volatility_state=VolatilityState.NORMAL,
        market_breadth_state=BreadthState.HEALTHY,
        market_liquidity_state=LiquidityState.NORMAL,
        data_quality_score=0.8,
        symbol_views=[
            _view("AAPL", probability=0.62),
            SymbolQuantView(
                symbol="MSFT",
                trend_state=TrendState.UP,
                momentum_state=MomentumState.STEADY,
                volatility_state=VolatilityState.NORMAL,
                liquidity_state=LiquidityState.NORMAL,
                probability_estimate=0.8,
                probability_basis="no zone",
            ),
        ],
    ).model_dump(mode="json")
    proposed = proposed_from_quant(payload, "RISK_ON")
    assert "AAPL" in proposed
    assert "MSFT" not in proposed


@pytest.mark.asyncio
async def test_picks_selected_and_rejected(session: AsyncSession) -> None:
    analysis = _analysis(
        views=[_view("AAPL"), _view("MSFT")],
        symbol_actions=[
            SymbolActionPlan(
                symbol="AAPL",
                action=SymbolAction.BUY,
                confidence=70,
                target_position_pct=5,
                thesis="AAPL breakout",
                invalidation="lose 98",
                stop_loss=98,
            )
        ],
    )
    await AuditService(session).persist_analysis(analysis)
    await session.commit()

    out = await PicksService(session).build(venue="US", limit=10)
    by_sym = {r["symbol"]: r for r in out["rows"]}
    assert by_sym["AAPL"]["status"] == "selected"
    assert "breakout" in by_sym["AAPL"]["reason"]
    assert by_sym["MSFT"]["status"] == "rejected"
    assert out["counts"]["selected"] == 1
    assert out["counts"]["rejected"] == 1


@pytest.mark.asyncio
async def test_picks_hold_keeps_symbol(session: AsyncSession) -> None:
    analysis = _analysis(
        views=[],
        portfolio_action=PortfolioAction.HOLD,
        symbol_actions=[
            SymbolActionPlan(
                symbol="BHP",
                action=SymbolAction.HOLD,
                confidence=55,
                target_position_pct=10,
                thesis="단기: hold on sideways/steady",
                invalidation="break swing",
            )
        ],
    )
    await AuditService(session).persist_analysis(analysis)
    await session.commit()

    out = await PicksService(session).build(limit=10)
    row = next(r for r in out["rows"] if r["symbol"] == "BHP")
    assert row["status"] == "hold"
    assert "sideways" in row["reason"]
    assert all(r["symbol"] != "—" for r in out["rows"])


@pytest.mark.asyncio
async def test_picks_no_trade_book_row(session: AsyncSession) -> None:
    analysis = _analysis(
        views=[],
        portfolio_action=PortfolioAction.NO_TRADE,
        reason_not_to_trade="halted tape",
        prefer_no_trade=True,
    )
    await AuditService(session).persist_analysis(analysis)
    await session.commit()

    out = await PicksService(session).build(limit=10)
    assert len(out["rows"]) == 1
    row = out["rows"][0]
    assert row["status"] == "rejected"
    assert "halted tape" in row["reason"]


@pytest.mark.asyncio
async def test_picks_execution_block_overrides_cio_pick(session: AsyncSession) -> None:
    analysis = _analysis(
        views=[_view("AAPL")],
        symbol_actions=[
            SymbolActionPlan(
                symbol="AAPL",
                action=SymbolAction.BUY,
                confidence=70,
                target_position_pct=5,
                thesis="AAPL breakout",
                invalidation="lose 98",
                stop_loss=98,
            )
        ],
    )
    await AuditService(session).persist_analysis(analysis)
    session.add(
        OrderIntent(
            decision_id=analysis.cio.decision_id,
            symbol="AAPL",
            intent_type="OPEN_LONG",
            side="buy",
            status=IntentStatus.RISK_REJECTED.value,
            thesis="AAPL breakout",
            metadata_json={"validation_rejections": ["hard_veto:daily_loss"]},
        )
    )
    await session.commit()

    out = await PicksService(session).build(limit=10)
    row = next(r for r in out["rows"] if r["symbol"] == "AAPL")
    assert row["status"] == "rejected"
    assert row["source"] == "execution"
    assert "daily_loss" in row["reason"]
    assert "breakout" in row["reason"]


def test_rows_for_decision_uses_devil_when_cio_passes() -> None:
    rec = CIODecisionRecord(
        decision_id=uuid4(),
        decision_timestamp=NOW,
        market_regime=MarketRegime.RISK_ON.value,
        portfolio_action=PortfolioAction.HOLD.value,
        payload={"symbol_actions": [], "venue": "US"},
        risk_approval=True,
        reason_not_to_trade=None,
    )
    quant = QuantStrategistOutput(
        timestamp=NOW,
        market_trend_state=TrendState.UP,
        market_momentum_state=MomentumState.STEADY,
        market_volatility_state=VolatilityState.NORMAL,
        market_breadth_state=BreadthState.HEALTHY,
        market_liquidity_state=LiquidityState.NORMAL,
        data_quality_score=0.8,
        symbol_views=[_view("BHP")],
    ).model_dump(mode="json")
    rows = rows_for_decision(
        rec,
        quant_payload=quant,
        risk_payload={"hard_vetoes": []},
        devil_payload={
            "prefer_no_trade": True,
            "prefer_no_trade_rationale": "wait for confirmation",
        },
        intents=[],
    )
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BHP"
    assert rows[0]["status"] == "rejected"
    assert rows[0]["source"] == "devil"
    assert "confirmation" in rows[0]["reason"]


def test_rows_for_decision_rejects_nameless_scale_in() -> None:
    rec = CIODecisionRecord(
        decision_id=uuid4(),
        decision_timestamp=NOW,
        market_regime=MarketRegime.RISK_ON.value,
        portfolio_action=PortfolioAction.SCALE_IN.value,
        payload={"symbol_actions": [], "venue": "AU"},
        risk_approval=True,
        reason_not_to_trade=None,
    )
    rows = rows_for_decision(
        rec,
        quant_payload={"symbol_views": []},
        risk_payload={"hard_vetoes": []},
        devil_payload={},
        intents=[],
    )
    assert len(rows) == 1
    assert rows[0]["symbol"] == "—"
    assert rows[0]["status"] == "rejected"
    assert rows[0]["action"] == "SCALE_IN"
