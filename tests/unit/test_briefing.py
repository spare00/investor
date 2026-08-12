"""Tests for daily CIO briefing shaping + service."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agents.pipeline import AnalysisBundle
from app.core.database import Base
from app.models import DailyWorkflowRun, IntradayAnalysisRun, IntradayDecisionRecord
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
from app.schemas.market_intelligence import MarketEvent
from app.services.audit import AuditService
from app.services.briefing import (
    BriefingService,
    session_day_bounds_utc,
    summarize_macro,
    summarize_mi,
)


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


def _analysis(workflow_id=None) -> AnalysisBundle:
    wf = workflow_id or uuid4()
    return AnalysisBundle(
        workflow_id=wf,
        market_intelligence=MarketIntelligenceOutput(
            timestamp=NOW,
            data_quality_score=0.8,
            market_events=[
                MarketEvent(
                    headline="Fed holds",
                    source="Reuters",
                    published_at=NOW,
                    category="fed",
                    sentiment="neutral",
                    importance=4,
                    symbols=["SPY"],
                )
            ],
            top_market_themes=["rates", "ai"],
        ),
        macro=MacroStrategistOutput(
            timestamp=NOW,
            market_regime=MarketRegime.RISK_ON,
            confidence=0.7,
            bullish_factors=["soft landing"],
            bearish_factors=["sticky services"],
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
        ),
        risk=RiskManagerOutput(
            timestamp=NOW,
            overall_verdict=RiskVerdict.APPROVED,
            cash_pct=80,
            gross_exposure_pct=20,
        ),
        devil=DevilsAdvocateOutput(
            timestamp=NOW,
            strongest_reason_thesis_is_wrong="priced in",
            information_already_in_price=False,
            information_already_in_price_rationale="n/a",
            opposing_market_scenario="fade",
            prefer_no_trade=False,
            prefer_no_trade_rationale="ok",
            challenge_score=0.4,
        ),
        cio=CIODecision(
            decision_id=uuid4(),
            timestamp=NOW,
            market_regime=MarketRegime.RISK_ON,
            portfolio_action=PortfolioAction.SCALE_IN,
            symbol_actions=[],
            cash_target_pct=70,
            risk_approval=True,
        ),
        completed_at=NOW,
    )


def test_summarize_helpers() -> None:
    mi = summarize_mi(
        {
            "top_market_themes": ["ai"],
            "market_events": [{"headline": "x", "importance": 3, "symbols": ["QQQ"]}],
            "data_quality_score": 0.5,
        }
    )
    assert mi["themes"] == ["ai"]
    assert mi["events"][0]["headline"] == "x"
    macro = summarize_macro({"market_regime": "RISK_ON", "bullish_factors": ["a"]})
    assert macro["market_regime"] == "RISK_ON"


def test_session_day_bounds() -> None:
    start, end = session_day_bounds_utc("2026-08-06")
    assert start.tzinfo is not None
    assert (end - start).total_seconds() == 86400


def test_session_day_bounds_asx_includes_morning_bne() -> None:
    """AU intraday ticks (e.g. 10:19 BNE) must fall inside the ASX session_date window."""
    start, end = session_day_bounds_utc("2026-08-12", calendar_name="ASX")
    # 10:19 BNE on 2026-08-12 = 00:19 UTC same calendar date
    tick = datetime(2026, 8, 12, 0, 19, tzinfo=UTC)
    assert start <= tick < end
    # 08:01 BNE premarket = prior UTC evening but still ASX session_date 2026-08-12
    pre = datetime(2026, 8, 11, 22, 1, tzinfo=UTC)
    assert start <= pre < end


def test_session_day_bounds_nyse_excludes_early_bne_tick() -> None:
    """Same 10:19 BNE tick is outside a US ET session_date window (US fix path)."""
    start, end = session_day_bounds_utc("2026-08-12", calendar_name="NYSE")
    tick = datetime(2026, 8, 12, 0, 19, tzinfo=UTC)
    assert not (start <= tick < end)


@pytest.mark.asyncio
async def test_briefing_service_assembles_premarket(session: AsyncSession) -> None:
    analysis = _analysis()
    await AuditService(session).persist_analysis(analysis)
    run = DailyWorkflowRun(
        id=uuid4(),
        session_date="2026-08-06",
        calendar_name="NYSE",
        current_state="MARKET_OPEN",
        status="running",
        analysis_workflow_run_id=analysis.workflow_id,
        latest_decision_id=analysis.cio.decision_id,
        metadata_json={
            "cio_action": "SCALE_IN",
            "risk_verdict": "approved",
            "intent_count": 1,
        },
    )
    session.add(run)

    analysis_run = IntradayAnalysisRun(
        id=uuid4(),
        status="COMPLETED",
        mode="paper_automated",
        trigger_event_ids=[],
        payload={"cio_action": "HOLD"},
    )
    session.add(analysis_run)
    session.add(
        IntradayDecisionRecord(
            id=uuid4(),
            analysis_run_id=analysis_run.id,
            as_of=NOW,
            market_regime="RISK_ON",
            thesis_status="INTACT",
            portfolio_action="HOLD",
            symbol_actions=[{"symbol": "QQQ", "action": "HOLD", "confidence": 55}],
            risk_approval=True,
            risk_conditions=[],
        )
    )
    await session.flush()

    briefing = await BriefingService(session).build(session_date="2026-08-06")
    assert briefing["available"] is True
    assert briefing["completeness"]["complete"] is True
    agents = {a["agent"]: a for a in briefing["premarket"]["agents"]}
    assert agents["macro_strategist"]["summary"]["market_regime"] == "RISK_ON"
    assert agents["market_intelligence"]["summary"]["themes"] == ["rates", "ai"]
    assert briefing["premarket"]["cio"]["portfolio_action"] == "SCALE_IN"
    assert len(briefing["intraday"]) == 1
    assert briefing["intraday"][0]["portfolio_action"] == "HOLD"
    assert len(briefing["session_analyses"]) >= 1


@pytest.mark.asyncio
async def test_briefing_risk_verdict_falls_back_to_agent(
    session: AsyncSession,
) -> None:
    """Summary strip should mirror Risk Manager materials when meta is sparse."""
    analysis = _analysis()
    await AuditService(session).persist_analysis(analysis)
    session.add(
        DailyWorkflowRun(
            id=uuid4(),
            session_date="2026-08-07",
            calendar_name="NYSE",
            current_state="PREMARKET_PREPARATION",
            status="running",
            analysis_workflow_run_id=analysis.workflow_id,
            latest_decision_id=analysis.cio.decision_id,
            metadata_json={},  # no risk_verdict / cio_action persisted
        )
    )
    await session.flush()

    briefing = await BriefingService(session).build(session_date="2026-08-07")
    assert briefing["daily_workflow"]["risk_verdict"] == "approved"
    assert briefing["daily_workflow"]["cio_action"] == "SCALE_IN"


@pytest.mark.asyncio
async def test_briefing_prefers_asx_intraday_over_premarket(session: AsyncSession) -> None:
    """Scheduled AU intraday must surface in summary, not stick on premarket."""

    pre_wf = uuid4()
    intra_wf = uuid4()
    pre_at = datetime(2026, 8, 11, 22, 1, tzinfo=UTC)  # 08:01 BNE
    intra_at = datetime(2026, 8, 12, 0, 19, tzinfo=UTC)  # 10:19 BNE

    for wf, ts, action in (
        (pre_wf, pre_at, PortfolioAction.NO_TRADE),
        (intra_wf, intra_at, PortfolioAction.HOLD),
    ):
        analysis = _analysis(workflow_id=wf)
        analysis.cio.portfolio_action = action
        await AuditService(session).persist_analysis(analysis)

    run = DailyWorkflowRun(
        id=uuid4(),
        session_date="2026-08-12",
        calendar_name="ASX",
        current_state="INTRADAY",
        status="running",
        analysis_workflow_run_id=pre_wf,
        latest_decision_id=analysis.cio.decision_id,
        metadata_json={
            "last_briefing_workflow_id": str(intra_wf),
            "last_briefing_kind": "intraday",
            "last_briefing_at": intra_at.isoformat(),
            "cio_action": "HOLD",
            "risk_verdict": "approved",
        },
    )
    session.add(run)
    await session.flush()

    briefing = await BriefingService(session).build(
        session_date="2026-08-12", calendar_name="ASX"
    )
    assert briefing["materials"]["kind"] == "intraday"
    assert briefing["materials"]["workflow_id"] == str(intra_wf)
    assert briefing["daily_workflow"]["cio_action"] == "HOLD"


@pytest.mark.asyncio
async def test_briefing_finds_manual_intraday_workflow(session: AsyncSession) -> None:
    """Dashboard Intraday Eval persists under a fresh workflow_id not on daily.analysis_workflow_run_id."""
    analysis = _analysis()
    await AuditService(session).persist_analysis(analysis)
    # Daily run points at a different (empty) workflow id — like production before linking.
    run = DailyWorkflowRun(
        id=uuid4(),
        session_date="2026-08-06",
        calendar_name="NYSE",
        current_state="INTRADAY",
        status="running",
        analysis_workflow_run_id=uuid4(),
        metadata_json={},
    )
    session.add(run)
    await session.flush()

    briefing = await BriefingService(session).build(session_date="2026-08-06")
    assert briefing["completeness"]["complete"] is True
    assert briefing["premarket"]["workflow_id"] == str(analysis.workflow_id)
    assert briefing["session_analyses"][0]["workflow_id"] == str(analysis.workflow_id)
