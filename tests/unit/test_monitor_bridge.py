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
from app.models import IntradayEvent, PositionLifecycle, ScheduledJobRecord
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
    events = list((await session.execute(select(IntradayEvent))).scalars().all())
    assert any(e.event_type == "STOP_TRIGGERED" for e in events)
    # Protective exits are submitted by the monitor; they must not fan into CIO.


@pytest.mark.asyncio
async def test_evaluate_intraday_flattens_leftover_overnight_once(
    session: AsyncSession,
) -> None:
    from app.core.config import Settings, TradingMode

    settings = Settings(
        app_env="test",
        trading_mode=TradingMode.PAPER,
        broker_environment="paper",
        enable_broker_orders=True,
        enable_automated_execution=False,
        require_manual_order_approval=False,
        auto_execute_force_close=False,
        intraday_operation_mode="PAPER_AUTOMATED",
        enable_intraday_monitoring=False,
        enable_intraday_agent_reanalysis=False,
        enable_scheduler=False,
        default_closing_policy="CLOSE_INTRADAY_ONLY",
    )
    svc = DailyWorkflowService(session, settings=settings)
    await svc.prepare(session_date="2026-08-03")
    run = await svc.get_current("2026-08-03")
    assert run is not None
    run.current_state = DailyWorkflowState.INTRADAY.value
    session.add(
        PositionLifecycle(
            id=uuid4(),
            symbol="QQQ",
            status="OPEN",
            quantity=8,
            average_entry_price=400,
            current_price=405,
            overnight_allowed=False,
            venue="US",
            exit_policy={},
        )
    )
    await session.flush()
    now = datetime(2026, 8, 3, 17, 0, tzinfo=UTC)
    first = await svc.evaluate_intraday(
        session_date="2026-08-03", trigger="interval", now=now, fake_llm=True
    )
    meta = dict(first.get("metadata") or {})
    stamped = meta.get("leftover_intraday_flatten_at")
    leftover = meta.get("leftover_intraday_flatten")
    assert stamped
    assert leftover is not None
    assert leftover.get("intent_ids")
    second = await svc.evaluate_intraday(
        session_date="2026-08-03",
        trigger="interval",
        now=now + timedelta(minutes=20),
        fake_llm=True,
    )
    assert (second.get("metadata") or {}).get("leftover_intraday_flatten_at") == stamped
    assert second["intraday"].get("leftover_flatten") is None


@pytest.mark.asyncio
async def test_evaluate_intraday_flattens_leftover_after_hours(
    session: AsyncSession,
) -> None:
    from app.core.config import Settings, TradingMode

    settings = Settings(
        app_env="test",
        trading_mode=TradingMode.PAPER,
        broker_environment="paper",
        enable_broker_orders=True,
        enable_automated_execution=False,
        require_manual_order_approval=False,
        auto_execute_force_close=False,
        intraday_operation_mode="PAPER_AUTOMATED",
        enable_intraday_monitoring=False,
        enable_intraday_agent_reanalysis=False,
        enable_scheduler=False,
        default_closing_policy="CLOSE_INTRADAY_ONLY",
    )
    svc = DailyWorkflowService(session, settings=settings)
    await svc.prepare(session_date="2026-08-03")
    run = await svc.get_current("2026-08-03")
    assert run is not None
    run.current_state = DailyWorkflowState.INTRADAY.value
    session.add(
        PositionLifecycle(
            id=uuid4(),
            symbol="QQQ",
            status="OPEN",
            quantity=8,
            average_entry_price=400,
            current_price=405,
            overnight_allowed=False,
            venue="US",
            exit_policy={"horizon": "day"},
        )
    )
    await session.flush()
    now = datetime(2026, 8, 4, 1, 0, tzinfo=UTC)
    out = await svc.evaluate_intraday(
        session_date="2026-08-03", trigger="interval", now=now, fake_llm=True
    )
    leftover = (out.get("metadata") or {}).get("leftover_intraday_flatten") or (
        out.get("intraday") or {}
    ).get("leftover_flatten")
    assert leftover is not None
    assert leftover.get("intent_ids")


@pytest.mark.asyncio
async def test_retry_missed_session_exits_after_hours(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.brokers.mock import MockBroker
    from app.core.config import Settings, TradingMode

    settings = Settings(
        app_env="test",
        trading_mode=TradingMode.PAPER,
        broker_environment="paper",
        broker_provider="mock",
        enable_live_trading=False,
        enable_broker_orders=True,
        enable_automated_execution=True,
        require_manual_order_approval=False,
        auto_execute_force_close=True,
        auto_execute_hard_stops=True,
        enable_intraday_monitoring=True,
        enable_intraday_agent_reanalysis=False,
        enable_scheduler=False,
        intraday_operation_mode="PAPER_AUTOMATED",
        default_closing_policy="CLOSE_INTRADAY_ONLY",
        starting_cash=50_000.0,
    )
    broker = MockBroker(seed=11, starting_cash=50_000, allow_short=False)
    broker.prices["QQQ"] = 400.0
    broker.positions["QQQ"] = {
        "symbol": "QQQ",
        "qty": "8",
        "avg_entry_price": "390",
        "market_value": "3200",
        "unrealized_pl": "80",
        "side": "long",
    }
    monkeypatch.setattr("app.brokers.factory.get_broker", lambda _s=None: broker)
    monkeypatch.setattr("app.execution.order_manager.get_broker", lambda _s=None: broker)
    svc = DailyWorkflowService(session, settings=settings)
    await svc.prepare(session_date="2026-08-03")
    run = await svc.get_current("2026-08-03")
    assert run is not None
    run.current_state = DailyWorkflowState.CLOSING_WINDOW.value
    session.add(
        PositionLifecycle(
            id=uuid4(),
            symbol="QQQ",
            status="OPEN",
            quantity=8,
            average_entry_price=400,
            current_price=400,
            overnight_allowed=False,
            venue="US",
            exit_policy={"horizon": "day"},
        )
    )
    await session.flush()
    now = datetime(2026, 8, 4, 1, 0, tzinfo=UTC)
    out = await svc.retry_missed_session_exits(now=now, session_date="2026-08-03")
    assert out.get("skipped") is False
    assert int(out.get("orders_submitted") or 0) >= 1


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
    assert "force_close" in review or "force_close_error" in review
    queued = (review.get("decision_eval") or {}).get("queued")
    assert queued and queued.endswith(":postmarket_eval")
    jobs = await svc.planned_jobs("2026-08-03")
    assert any(j["job_key"].endswith(":postmarket_eval") for j in jobs)


@pytest.mark.asyncio
async def test_retry_incomplete_postmarket_after_hours(session: AsyncSession) -> None:
    svc = DailyWorkflowService(session, settings=get_settings())
    await svc.prepare(session_date="2026-08-03")
    run = await svc.get_current("2026-08-03")
    assert run is not None
    run.current_state = DailyWorkflowState.CLOSING_WINDOW.value
    row = (
        await session.execute(
            select(ScheduledJobRecord).where(
                ScheduledJobRecord.job_key == "US:postmarket_review",
                ScheduledJobRecord.session_date == "2026-08-03",
            )
        )
    ).scalar_one()
    row.status = "failed"
    row.error = "stale_running_reaped:480s"
    await session.flush()
    now = datetime(2026, 8, 4, 1, 0, tzinfo=UTC)
    out = await svc.retry_incomplete_postmarket(now=now, session_date="2026-08-03")
    assert out.get("skipped") is False
    assert out["current_state"] == DailyWorkflowState.COMPLETED.value
    await session.refresh(row)
    assert row.status == "completed"
    assert row.error is None


@pytest.mark.asyncio
async def test_retry_incomplete_postmarket_prior_session_during_premarket(
    session: AsyncSession,
) -> None:
    """A stuck close must still finish after the next day's premarket has started."""
    svc = DailyWorkflowService(session, settings=get_settings())
    await svc.prepare(session_date="2026-08-03")
    run = await svc.get_current("2026-08-03")
    assert run is not None
    run.current_state = DailyWorkflowState.CLOSING_WINDOW.value
    await session.flush()
    now = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    out = await svc.retry_incomplete_postmarket(now=now)
    assert out.get("skipped") is False
    assert out["current_state"] == DailyWorkflowState.COMPLETED.value
    review = out.get("review") or {}
    assert review.get("session_date") == "2026-08-03"


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


def _cio_row(ts: datetime, action: str = "HOLD", *, venue: str | None = None):
    from app.models import CIODecisionRecord

    did = uuid4()
    payload: dict = {"portfolio_action": action, "symbol_actions": []}
    if venue:
        payload["venue"] = venue
    return did, CIODecisionRecord(
        id=uuid4(),
        decision_id=did,
        decision_timestamp=ts,
        market_regime="neutral",
        portfolio_action=action,
        payload=payload,
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
        venue="US",
    )
    eval_ids = {e["decision_id"] for e in out["evaluations"]}
    assert str(ids[0]) not in eval_ids
    assert out["decisions_processed"] == 2
    assert out["remaining_decisions"] == 0


@pytest.mark.asyncio
async def test_postmarket_eval_drains_then_reschedules(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.workflow import daily as daily_mod

    monkeypatch.setattr(daily_mod, "POSTMARKET_EVAL_CHUNK", 1)
    monkeypatch.setattr(daily_mod, "POSTMARKET_EVAL_MAX_CHUNKS_PER_JOB", 1)
    eval_now = datetime(2026, 8, 4, 2, 0, tzinfo=UTC)
    for i in range(2):
        _did, row = _cio_row(eval_now - timedelta(minutes=i), venue="US")
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
        ":postmarket_eval"
    )
    first = await svc.run_postmarket_eval(session_date="2026-08-03", now=eval_now)
    ev = first["eval"]
    assert ev["decisions_processed"] == 1
    assert ev["remaining_decisions"] == 1
    assert ev["reschedule"] is True
    monkeypatch.setattr(daily_mod, "POSTMARKET_EVAL_MAX_CHUNKS_PER_JOB", 40)
    second = await svc.run_postmarket_eval(session_date="2026-08-03", now=eval_now)
    assert second["eval"]["decisions_processed"] == 1
    assert second["eval"]["remaining_decisions"] == 0
    assert second["eval"]["reschedule"] is False


@pytest.mark.asyncio
async def test_postmarket_eval_yields_during_regular_session(
    session: AsyncSession,
) -> None:
    svc = DailyWorkflowService(session, settings=get_settings())
    await svc.prepare(session_date="2026-08-03")
    run = await svc.get_current("2026-08-03")
    assert run is not None
    run.current_state = DailyWorkflowState.COMPLETED.value
    await session.flush()
    regular = datetime(2026, 8, 3, 17, 0, tzinfo=UTC)
    out = await svc.run_postmarket_eval(session_date="2026-08-03", now=regular)
    ev = out["eval"]
    assert ev["reschedule"] is True
    assert "REGULAR" in str(ev.get("skipped") or ev.get("phase") or "")
    assert ev.get("decisions_processed") in (None, 0)


@pytest.mark.asyncio
async def test_evaluate_decisions_batch_scopes_to_venue(session: AsyncSession) -> None:
    from app.performance.service import PerformanceService

    now = datetime.now(UTC)
    us_id, us_row = _cio_row(now, venue="US")
    au_id, au_row = _cio_row(now - timedelta(minutes=1), venue="AU")
    session.add_all([us_row, au_row])
    await session.flush()
    perf = PerformanceService(session, settings=get_settings())
    out = await perf.evaluate_decisions_batch(
        now - timedelta(hours=1),
        now + timedelta(hours=1),
        limit=10,
        persist=True,
        skip_evaluated=True,
        venue="AU",
    )
    eval_ids = {e["decision_id"] for e in out["evaluations"]}
    assert str(au_id) in eval_ids
    assert str(us_id) not in eval_ids
    assert out["decisions_processed"] == 1
    assert out["remaining_decisions"] == 0


@pytest.mark.asyncio
async def test_pending_refresh_is_venue_scoped(session: AsyncSession) -> None:
    from app.models import DecisionEvaluationRecord
    from app.performance.service import PerformanceService

    now = datetime.now(UTC)
    us_id, us_row = _cio_row(now, venue="US")
    au_id, au_row = _cio_row(now - timedelta(minutes=1), venue="AU")
    session.add_all([us_row, au_row])
    session.add_all(
        [
            DecisionEvaluationRecord(
                id=uuid4(),
                decision_id=us_id,
                decision_type="cio",
                action="HOLD",
                decision_price=0,
                evaluation_horizon="1d",
                evaluated_at=now,
                status="PENDING",
                payload={"venue": "US"},
            ),
            DecisionEvaluationRecord(
                id=uuid4(),
                decision_id=au_id,
                decision_type="cio",
                action="HOLD",
                decision_price=0,
                evaluation_horizon="1d",
                evaluated_at=now,
                status="PENDING",
                payload={"venue": "AU"},
            ),
        ]
    )
    await session.flush()
    perf = PerformanceService(session, settings=get_settings())
    deleted = await perf.refresh_pending_evaluations(
        now - timedelta(hours=1), now + timedelta(hours=1), venue="AU"
    )
    assert deleted == 1
    left = list((await session.execute(select(DecisionEvaluationRecord))).scalars().all())
    assert len(left) == 1
    assert left[0].decision_id == us_id
    assert left[0].status == "PENDING"
