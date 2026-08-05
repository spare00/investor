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
    assert client.get("/health").json()["phase"] == 7
    dash = client.get("/dashboard")
    assert dash.status_code == 200
    assert b"Investor Ops" in dash.content
    assert b"kpi-grid" in dash.content
    assert b"function kpiFromMetric" in dash.content
    assert b"Raw JSON" in dash.content
    assert b"usSessionChip" in dash.content
    assert b"renderUsSession" in dash.content
    assert b"renderIntradayCadence" in dash.content
    assert b"universePaused" in dash.content
    assert b"refreshStrip" in dash.content
    assert b"Promise.allSettled" in dash.content
    summary = client.get("/dashboard/summary")
    assert summary.status_code == 200
    body = summary.json()
    assert "force_close" in body
    assert "hard_stop" in body
    assert "monitor_positions" in body
    assert "pending_events" in body
    assert "llm_budget" in body
    assert "latest_settlement" in body
    assert "latest_reconciliation" in body
    assert "latest_recovery" in body
    assert "active_alerts" in body
    assert "overnight_reviews" in body
    assert "session_jobs" in body
    assert "universe" in body
    assert b"renderMonitor" in dash.content
    assert b"renderSettlement" in dash.content
    assert b"renderRecovery" in dash.content
    assert b"renderActiveAlerts" in dash.content
    assert b"ackAlert" in dash.content
    assert b"overnightDetail" in dash.content
    assert b"renderLlmBudgetPanel" in dash.content
    assert b"overviewOpsStrip" in dash.content
    assert b"renderOverviewOpsStrip" in dash.content
    assert b"agentPerfNote" in dash.content
    assert b"Horizon book" in dash.content
    assert b"Startup / Intraday Recovery" in dash.content
    assert b"Active Alerts" in dash.content
    assert b"Position Monitor / Hard Stops" in dash.content
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert b"investor_" in metrics.content or b"python_" in metrics.content
