"""Phase 7 dashboard / metrics tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agents.pipeline import AnalysisBundle
from app.core.database import Base
from app.core.metrics import WORKFLOW_RUNS, metrics_payload
from app.main import app
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
from app.schemas.common import (
    BreadthState,
    LiquidityState,
    MomentumState,
    TrendState,
    VolatilityState,
)
from app.services.audit import AuditService
from app.services.llm import StubLLMClient


NOW = datetime(2026, 8, 3, 16, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


def _analysis() -> AnalysisBundle:
    return AnalysisBundle(
        workflow_id=uuid4(),
        market_intelligence=MarketIntelligenceOutput(
            timestamp=NOW, data_quality_score=0.8, market_events=[], top_market_themes=[]
        ),
        macro=MacroStrategistOutput(
            timestamp=NOW,
            market_regime=MarketRegime.NEUTRAL,
            confidence=0.5,
            data_quality_score=0.8,
        ),
        quant=QuantStrategistOutput(
            timestamp=NOW,
            market_trend_state=TrendState.SIDEWAYS,
            market_momentum_state=MomentumState.STEADY,
            market_volatility_state=VolatilityState.NORMAL,
            market_breadth_state=BreadthState.MIXED,
            market_liquidity_state=LiquidityState.NORMAL,
            data_quality_score=0.8,
        ),
        risk=RiskManagerOutput(
            timestamp=NOW,
            overall_verdict=RiskVerdict.APPROVED,
            cash_pct=80,
            gross_exposure_pct=20,
        ),
        devil=DevilsAdvocateOutput(
            timestamp=NOW,
            strongest_reason_thesis_is_wrong="x",
            information_already_in_price=False,
            information_already_in_price_rationale="n",
            opposing_market_scenario="fade",
            prefer_no_trade=True,
            prefer_no_trade_rationale="wait",
            challenge_score=0.5,
        ),
        cio=CIODecision(
            timestamp=NOW,
            market_regime=MarketRegime.NEUTRAL,
            portfolio_action=PortfolioAction.NO_TRADE,
            cash_target_pct=80,
            risk_approval=True,
            reason_not_to_trade="dashboard test",
        ),
        completed_at=NOW,
    )


@pytest.mark.asyncio
async def test_audit_persist_and_metrics(session: AsyncSession) -> None:
    await AuditService(session).persist_analysis(_analysis())
    await session.commit()
    body, ctype = metrics_payload()
    assert b"investor_app" in body or b"investor_workflow" in body or b"python_info" in body
    assert "text/plain" in ctype
    WORKFLOW_RUNS.labels(kind="premarket", outcome="ok").inc()


def test_dashboard_routes_exist() -> None:
    client = TestClient(app)
    assert client.get("/health").json()["phase"] == 6
    dash = client.get("/dashboard")
    assert dash.status_code == 200
    assert b"Investor Ops" in dash.content
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert b"investor_" in metrics.content or b"python_" in metrics.content
