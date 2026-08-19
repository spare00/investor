"""Unattended monitor → evaluate_intraday risk escalation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import clear_settings_cache, get_settings
from app.core.database import Base
import app.models  # noqa: F401
from app.execution.safety_controls import trading_controls
from app.models import IntradayEvent, PositionLifecycle
from app.workflow.daily import DailyWorkflowService
from app.workflow.states import DailyWorkflowState


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
def _reset() -> None:
    clear_settings_cache()
    trading_controls.clear_emergency()
    trading_controls.resume()
    yield
    clear_settings_cache()
    trading_controls.clear_emergency()
    trading_controls.resume()


@pytest.mark.asyncio
async def test_evaluate_intraday_escalates_stop_to_risk_change(session: AsyncSession) -> None:
    svc = DailyWorkflowService(session, settings=get_settings())
    await svc.prepare(session_date="2026-08-03")
    run = await svc.get_current("2026-08-03")
    assert run is not None
    run.current_state = DailyWorkflowState.INTRADAY.value
    session.add(
        PositionLifecycle(
            id=uuid4(),
            symbol="QQQ",
            status="OPEN",
            quantity=10,
            average_entry_price=400,
            current_price=390,
            stop_price=395,
            overnight_allowed=False,
            exit_policy={},
        )
    )
    await session.flush()

    now = datetime(2026, 8, 3, 17, 0, tzinfo=UTC)  # regular session
    out = await svc.evaluate_intraday(
        session_date="2026-08-03", trigger="interval", now=now, fake_llm=True
    )
    intra = out["intraday"]
    assert intra.get("monitor")
    assert (intra["monitor"].get("actionable") or 0) >= 1
    assert intra.get("trigger") == "risk_change"
    events = list((await session.execute(select(IntradayEvent))).scalars().all())
    assert any(e.event_type == "STOP_TRIGGERED" for e in events)


@pytest.mark.asyncio
async def test_postmarket_runs_settlement(session: AsyncSession) -> None:
    svc = DailyWorkflowService(session, settings=get_settings())
    await svc.prepare(session_date="2026-08-03")
    run = await svc.get_current("2026-08-03")
    assert run is not None
    run.current_state = DailyWorkflowState.CLOSING_WINDOW.value
    await session.flush()
    post = await svc.run_postmarket(session_date="2026-08-03")
    assert post["current_state"] == DailyWorkflowState.COMPLETED.value
    review = post["review"]
    # Settlement may soft-fail without broker; either settlement or settlement_error is set.
    assert "settlement" in review or "settlement_error" in review
    queued = (review.get("decision_eval") or {}).get("queued")
    assert queued and queued.endswith("postmarket_eval_0")
    jobs = await svc.planned_jobs("2026-08-03")
    assert any(j["job_key"].endswith("postmarket_eval_0") for j in jobs)


@pytest.mark.asyncio
async def test_postmarket_completes_when_a_step_times_out(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from app.workflow import daily as daily_mod

    async def _hang(*args: object, **kwargs: object) -> dict:
        await asyncio.sleep(30)
        return {}

    monkeypatch.setitem(daily_mod.POSTMARKET_STEP_TIMEOUTS_SECONDS, "settlement", 0.05)
    monkeypatch.setattr(
        "app.intraday.settlement.SettlementService.settle",
        _hang,
    )
    svc = DailyWorkflowService(session, settings=get_settings())
    await svc.prepare(session_date="2026-08-03")
    run = await svc.get_current("2026-08-03")
    assert run is not None
    run.current_state = DailyWorkflowState.CLOSING_WINDOW.value
    await session.flush()
    post = await svc.run_postmarket(session_date="2026-08-03")
    assert post["current_state"] == DailyWorkflowState.COMPLETED.value
    assert "timeout:settlement" in str((post.get("review") or {}).get("settlement_error") or "")


def _cio_row(ts: datetime, action: str = "HOLD"):
    from app.models import CIODecisionRecord

    did = uuid4()
    return did, CIODecisionRecord(
        id=uuid4(),
        decision_id=did,
        decision_timestamp=ts,
        market_regime="neutral",
        portfolio_action=action,
        payload={"portfolio_action": action, "symbol_actions": []},
        risk_approval=True,
    )


@pytest.mark.asyncio
async def test_evaluate_decisions_batch_skips_already_scored(session: AsyncSession) -> None:
    from app.models import DecisionEvaluationRecord
    from app.performance.service import PerformanceService

    now = datetime.now(UTC)
    ids = []
    for i in range(3):
        did, row = _cio_row(now - timedelta(minutes=i))
        ids.append(did)
        session.add(row)
    session.add(
        DecisionEvaluationRecord(
            id=uuid4(),
            decision_id=ids[0],
            decision_type="cio",
            action="HOLD",
            decision_price=0,
            evaluation_horizon="1d",
            evaluated_at=now,
            status="AVAILABLE",
            payload={},
        )
    )
    await session.flush()
    perf = PerformanceService(session, settings=get_settings())
    out = await perf.evaluate_decisions_batch(
        now - timedelta(hours=1),
        now + timedelta(hours=1),
        limit=10,
        persist=True,
        skip_evaluated=True,
    )
    eval_ids = {e["decision_id"] for e in out["evaluations"]}
    assert str(ids[0]) not in eval_ids
    assert out["decisions_processed"] == 2
    assert out["remaining_decisions"] == 0


@pytest.mark.asyncio
async def test_postmarket_eval_enqueues_next_chunk(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.workflow import daily as daily_mod

    monkeypatch.setattr(daily_mod, "POSTMARKET_EVAL_CHUNK", 1)
    now = datetime.now(UTC)
    for i in range(2):
        _did, row = _cio_row(now - timedelta(minutes=i))
        session.add(row)
    await session.flush()

    svc = DailyWorkflowService(session, settings=get_settings())
    await svc.prepare(session_date="2026-08-03")
    run = await svc.get_current("2026-08-03")
    assert run is not None
    run.current_state = DailyWorkflowState.CLOSING_WINDOW.value
    await session.flush()
    post = await svc.run_postmarket(session_date="2026-08-03")
    assert (post["review"].get("decision_eval") or {}).get("queued", "").endswith(
        "postmarket_eval_0"
    )
    first = await svc.run_postmarket_eval(session_date="2026-08-03", seq=0)
    ev = first["eval"]
    assert ev["decisions_processed"] == 1
    assert ev["remaining_decisions"] == 1
    assert str(ev.get("next_job") or "").endswith("postmarket_eval_1")
    jobs = await svc.planned_jobs("2026-08-03")
    assert any(j["job_key"].endswith("postmarket_eval_1") for j in jobs)
    second = await svc.run_postmarket_eval(session_date="2026-08-03", seq=1)
    assert second["eval"]["decisions_processed"] == 1
    assert second["eval"]["remaining_decisions"] == 0
    assert second["eval"]["next_job"] is None
