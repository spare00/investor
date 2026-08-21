"""Phase 3 state machine, lease, recovery, revalidation, safety tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import clear_settings_cache, get_settings
from app.core.database import Base
from app.core.scheduler import _scheduler_enabled, start_scheduler
from app.execution.ops_persistence import persist_trading_controls, restore_trading_controls
from app.execution.safety_controls import TradingControls, trading_controls
from app.models import DailyWorkflowRun  # noqa: F401 — register ORM tables
from app.workflow.closing import ClosingPolicyEngine
from app.workflow.daily import DailyWorkflowError, DailyWorkflowService
from app.workflow.lease import LeaseError, LeaseService
from app.workflow.recovery import RecoveryService
from app.workflow.states import (
    ClosingPolicy,
    DailyWorkflowState,
    assert_transition_allowed,
)

ET = ZoneInfo("America/New_York")


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
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_BROKER_ORDERS", "false")
    monkeypatch.setenv("ENABLE_AUTOMATED_EXECUTION", "false")
    monkeypatch.setenv("ENABLE_EXTERNAL_DATA", "false")
    monkeypatch.setenv("ENABLE_SCHEDULER", "false")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    clear_settings_cache()
    trading_controls.clear_emergency("test_reset")
    trading_controls.resume("test_reset")
    yield
    trading_controls.clear_emergency("test_reset")
    trading_controls.resume("test_reset")
    clear_settings_cache()


def test_illegal_transition_blocked() -> None:
    with pytest.raises(ValueError, match="illegal_transition"):
        assert_transition_allowed(
            DailyWorkflowState.PREMARKET_PREPARATION, DailyWorkflowState.COMPLETED
        )


def test_allowed_happy_path_transitions() -> None:
    path = [
        (DailyWorkflowState.PREMARKET_PREPARATION, DailyWorkflowState.PREMARKET_ANALYSIS),
        (DailyWorkflowState.PREMARKET_ANALYSIS, DailyWorkflowState.PREOPEN_REVALIDATION),
        (DailyWorkflowState.PREOPEN_REVALIDATION, DailyWorkflowState.MARKET_OPEN),
        (DailyWorkflowState.MARKET_OPEN, DailyWorkflowState.INTRADAY),
        (DailyWorkflowState.INTRADAY, DailyWorkflowState.CLOSING_WINDOW),
        (DailyWorkflowState.CLOSING_WINDOW, DailyWorkflowState.MARKET_CLOSED),
        (DailyWorkflowState.MARKET_CLOSED, DailyWorkflowState.POSTMARKET_REVIEW),
        (DailyWorkflowState.POSTMARKET_REVIEW, DailyWorkflowState.COMPLETED),
    ]
    for a, b in path:
        assert_transition_allowed(a, b)


@pytest.mark.asyncio
async def test_prepare_non_trading_day(session: AsyncSession) -> None:
    svc = DailyWorkflowService(session, settings=get_settings())
    result = await svc.prepare(session_date="2026-08-01")  # Saturday
    assert result["current_state"] == DailyWorkflowState.COMPLETED.value
    again = await svc.prepare(session_date="2026-08-01")
    assert again["note"] == "already_prepared"


@pytest.mark.asyncio
async def test_prepare_trading_day_plans_jobs(session: AsyncSession) -> None:
    svc = DailyWorkflowService(session, settings=get_settings())
    result = await svc.prepare(session_date="2026-08-03")
    assert result["current_state"] == DailyWorkflowState.PREMARKET_PREPARATION.value
    jobs = await svc.planned_jobs("2026-08-03")
    keys = {j["job_key"] for j in jobs}
    assert "US:premarket_analysis" in keys
    assert "US:closing_window" in keys
    assert "US:postmarket_review" in keys


@pytest.mark.asyncio
async def test_prepare_plans_dense_intraday_when_scalp_seeded(session: AsyncSession) -> None:
    """Seeded scalp + LLM budget → denser than legacy 30m fallback, not raw 2m flood."""
    from app.core.config import Settings

    settings = Settings(
        app_env="test",
        llm_runtime="cloud",
        llm_base_url="https://api.openai.com/v1",
        max_intraday_reanalyses=12,
        intraday_reevaluation_interval_minutes=30,
        enable_scheduler=False,
    )
    svc = DailyWorkflowService(session, settings=settings)
    await svc.prepare(session_date="2026-08-03")
    jobs = await svc.planned_jobs("2026-08-03")
    intra = [j for j in jobs if j["job_key"].startswith("US:intraday_eval_")]
    assert len(intra) >= 2
    t0 = datetime.fromisoformat(intra[0]["planned_at"])
    t1 = datetime.fromisoformat(intra[1]["planned_at"])
    if t0.tzinfo is None:
        t0 = t0.replace(tzinfo=UTC)
    if t1.tzinfo is None:
        t1 = t1.replace(tzinfo=UTC)
    gap = t1 - t0
    # Budget floor with max_intraday_reanalyses=12 → ~20m on a full session
    assert gap < timedelta(minutes=30)
    assert gap >= timedelta(minutes=10)
    assert 8 <= len(intra) <= 25
    from app.market.calendar import MarketCalendarService

    close = MarketCalendarService(settings).get_session(
        datetime(2026, 8, 3, tzinfo=UTC).date()
    ).regular_close
    assert close is not None
    last = max(datetime.fromisoformat(j["planned_at"]) for j in intra)
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    # Force-close window is the last 15 minutes; at least one tick must land in it.
    assert last >= close.astimezone(UTC) - timedelta(minutes=15)


@pytest.mark.asyncio
async def test_replan_intraday_jobs_after_horizon_change(session: AsyncSession) -> None:
    from app.models import WatchlistSymbol
    from sqlalchemy import select

    svc = DailyWorkflowService(session, settings=get_settings())
    await svc.prepare(session_date="2026-08-03")
    before = await svc.planned_jobs("2026-08-03")
    before_intra = [j for j in before if j["job_key"].startswith("US:intraday_eval_")]
    assert before_intra

    # Collapse active books to medium-only → coarser plan on replan.
    rows = list((await session.execute(select(WatchlistSymbol))).scalars().all())
    for r in rows:
        r.horizon = "medium"
        r.status = "active"
    await session.flush()

    mid_session = datetime(2026, 8, 3, 17, 0, tzinfo=UTC)  # ~13:00 ET
    out = await svc.replan_intraday_jobs(session_date="2026-08-03", now=mid_session)
    assert out["skipped"] is False
    assert out["purged"] >= 1
    after = await svc.planned_jobs("2026-08-03")
    after_planned = [
        j
        for j in after
        if j["job_key"].startswith("US:intraday_eval_") and j["status"] == "planned"
    ]
    assert after_planned
    # Medium + budget → fewer remaining ticks than original full-day scalp plan
    assert len(after_planned) < len(before_intra)


@pytest.mark.asyncio
async def test_early_close_job_uses_session_close(session: AsyncSession) -> None:
    svc = DailyWorkflowService(session, settings=get_settings())
    await svc.prepare(session_date="2026-11-27")
    jobs = await svc.planned_jobs("2026-11-27")
    closing = next(j for j in jobs if j["job_key"] == "US:closing_window")
    planned = datetime.fromisoformat(closing["planned_at"])
    if planned.tzinfo is None:
        planned = planned.replace(tzinfo=UTC)
    # Early close 13:00 ET → closing window 30m before = 12:30 ET
    local = planned.astimezone(ET)
    assert local.hour == 12
    assert local.minute == 30


@pytest.mark.asyncio
async def test_run_analysis_rejects_fixtures_when_execution_armed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENABLE_EXTERNAL_DATA", "false")
    monkeypatch.setenv("ENABLE_BROKER_ORDERS", "true")
    monkeypatch.setenv("ENABLE_AUTOMATED_EXECUTION", "true")
    clear_settings_cache()
    svc = DailyWorkflowService(session, settings=get_settings())
    await svc.prepare(session_date="2026-08-03")
    with pytest.raises(DailyWorkflowError, match="external_data_required_when_execution_armed"):
        await svc.run_analysis(session_date="2026-08-03", fake_llm=True)


@pytest.mark.asyncio
async def test_full_flow_with_fake_analysis(session: AsyncSession) -> None:
    svc = DailyWorkflowService(session, settings=get_settings())
    await svc.prepare(session_date="2026-08-03")
    analysis = await svc.run_analysis(session_date="2026-08-03", fake_llm=True)
    assert analysis["analysis"]["broker_orders_submitted"] is False
    assert analysis["current_state"] == DailyWorkflowState.PREOPEN_REVALIDATION.value
    now = datetime(2026, 8, 3, 13, 0, tzinfo=UTC)  # 09:00 EDT
    reval = await svc.revalidate(session_date="2026-08-03", now=now)
    assert reval["revalidation"]["result"] == "VALID"
    assert reval["current_state"] == DailyWorkflowState.INTRADAY.value
    intra = await svc.evaluate_intraday(
        session_date="2026-08-03", trigger="interval", now=now, fake_llm=True
    )
    assert intra["intraday"]["broker_orders"] is False
    closing = await svc.start_closing(
        session_date="2026-08-03", positions=[{"symbol": "SPY", "quantity": 1}]
    )
    assert closing["closing"]["broker_orders_allowed"] is False
    post = await svc.run_postmarket(session_date="2026-08-03")
    assert post["current_state"] == DailyWorkflowState.COMPLETED.value
    assert post["review"]["broker_orders_submitted"] is False


@pytest.mark.asyncio
async def test_pause_and_emergency_block_actions(session: AsyncSession) -> None:
    svc = DailyWorkflowService(session, settings=get_settings())
    await svc.prepare(session_date="2026-08-03")
    trading_controls.pause("test")
    with pytest.raises(DailyWorkflowError, match="paused"):
        await svc.run_analysis(session_date="2026-08-03", fake_llm=True)
    trading_controls.resume("test")
    trading_controls.emergency_stop("test")
    with pytest.raises(DailyWorkflowError, match="emergency"):
        await svc.run_analysis(session_date="2026-08-03", fake_llm=True)


@pytest.mark.asyncio
async def test_lease_acquire_conflict_and_expire(session: AsyncSession) -> None:
    leases = LeaseService(session, get_settings())
    await leases.acquire("k1", "owner-a")
    with pytest.raises(LeaseError):
        await leases.acquire("k1", "owner-b")
    row = await leases.acquire("k2", "a")
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.flush()
    n = await leases.reclaim_expired()
    assert n >= 1
    await leases.acquire("k2", "b")


@pytest.mark.asyncio
async def test_lease_heartbeat_owner_mismatch(session: AsyncSession) -> None:
    leases = LeaseService(session, get_settings())
    await leases.acquire("hb", "owner-a")
    await leases.heartbeat("hb", "owner-a")
    with pytest.raises(LeaseError, match="owner_mismatch"):
        await leases.heartbeat("hb", "owner-b")
    with pytest.raises(LeaseError, match="owner_mismatch"):
        await leases.release("hb", "owner-b")


@pytest.mark.asyncio
async def test_revalidation_hard_veto(session: AsyncSession) -> None:
    svc = DailyWorkflowService(session, settings=get_settings())
    await svc.prepare(session_date="2026-08-03")
    await svc.run_analysis(session_date="2026-08-03", fake_llm=True)
    now = datetime(2026, 8, 3, 13, 20, tzinfo=UTC)
    hard = await svc.revalidate(
        session_date="2026-08-03", now=now, fixture={"hard_veto": True}
    )
    assert hard["revalidation"]["result"] == "NO_TRADE"


@pytest.mark.asyncio
async def test_revalidation_stale_triggers_reanalysis(session: AsyncSession) -> None:
    svc = DailyWorkflowService(session, settings=get_settings())
    await svc.prepare(session_date="2026-08-04")
    await svc.run_analysis(session_date="2026-08-04", fake_llm=True)
    now = datetime(2026, 8, 4, 13, 0, tzinfo=UTC)
    r1 = await svc.revalidate(
        session_date="2026-08-04", now=now, fixture={"stale_data": True}, fake_llm=True
    )
    # Reanalysis must settle via a second revalidate in the same call — never leave
    # the session stuck in PREOPEN_REVALIDATION.
    assert "follow_up" in r1
    assert r1["current_state"] == DailyWorkflowState.INTRADAY.value
    assert r1.get("revalidation", {}).get("result") in {"VALID", "VALID_WITH_RESTRICTIONS", "NO_TRADE"}


@pytest.mark.asyncio
async def test_revalidation_market_events_do_not_stick_session(
    session: AsyncSession,
) -> None:
    svc = DailyWorkflowService(session, settings=get_settings())
    await svc.prepare(session_date="2026-08-04")
    await svc.run_analysis(session_date="2026-08-04", fake_llm=True)
    run = await svc.get_current("2026-08-04")
    assert run is not None
    meta = dict(run.metadata_json or {})
    meta["market_events"] = [
        {
            "requires_reanalysis": True,
            "detected_at": "2026-08-04T12:00:00+00:00",
            "effective_at": "2026-08-02T12:00:00+00:00",
            "event_type": "SEC_MATERIAL_FILING",
            "symbols": ["SPY"],
        }
    ]
    # Make events look "new" relative to a slightly earlier analysis stamp.
    meta["analysis_completed_at"] = "2026-08-04T11:00:00+00:00"
    run.metadata_json = meta
    await session.flush()
    now = datetime(2026, 8, 4, 13, 20, tzinfo=UTC)
    out = await svc.revalidate(session_date="2026-08-04", now=now, fake_llm=True)
    assert out["current_state"] == DailyWorkflowState.INTRADAY.value
    run2 = await svc.get_current("2026-08-04")
    assert run2 is not None
    events = (run2.metadata_json or {}).get("market_events") or []
    assert events and events[0].get("requires_reanalysis") is False


@pytest.mark.asyncio
async def test_catch_up_enters_intraday_after_open(session: AsyncSession) -> None:
    svc = DailyWorkflowService(session, settings=get_settings())
    await svc.prepare(session_date="2026-08-04")
    await svc.run_analysis(session_date="2026-08-04", fake_llm=True)
    # Stuck in PREOPEN (as Aug 4 production bug) while regular session is open.
    now = datetime(2026, 8, 4, 15, 0, tzinfo=UTC)  # 11:00 ET
    out = await svc.catch_up_to_intraday(session_date="2026-08-04", now=now, fake_llm=True)
    assert out["current_state"] == DailyWorkflowState.INTRADAY.value
    assert out["catch_up"]["skipped"] is False


@pytest.mark.asyncio
async def test_catch_up_marks_planned_premarket_jobs_completed(session: AsyncSession) -> None:
    from app.models import ScheduledJobRecord
    from sqlalchemy import select

    svc = DailyWorkflowService(session, settings=get_settings())
    await svc.prepare(session_date="2026-08-04")
    jobs = (
        await session.execute(
            select(ScheduledJobRecord).where(ScheduledJobRecord.session_date == "2026-08-04")
        )
    ).scalars().all()
    by_key = {j.job_key: j for j in jobs}
    assert by_key["US:premarket_analysis"].status == "planned"
    assert by_key["US:preopen_revalidation"].status == "planned"

    # Near open from PREMARKET_PREPARATION — catch-up runs analysis+revalidate.
    now = datetime(2026, 8, 4, 13, 25, tzinfo=UTC)  # 09:25 ET
    out = await svc.catch_up_to_intraday(
        session_date="2026-08-04", now=now, fake_llm=True, force=True
    )
    assert "analysis" in (out.get("catch_up") or {}).get("steps", [])
    assert "revalidate" in (out.get("catch_up") or {}).get("steps", [])
    await session.refresh(by_key["US:premarket_analysis"])
    await session.refresh(by_key["US:preopen_revalidation"])
    assert by_key["US:premarket_analysis"].status == "completed"
    assert by_key["US:preopen_revalidation"].status == "completed"
    assert "US:premarket_analysis" in (out.get("catch_up") or {}).get("jobs_marked", [])
    assert "US:preopen_revalidation" in (out.get("catch_up") or {}).get("jobs_marked", [])


@pytest.mark.asyncio
async def test_recovery_preserves_emergency(session: AsyncSession) -> None:
    trading_controls.emergency_stop("persist-me")
    await persist_trading_controls(session, trading_controls)
    fresh = TradingControls()
    assert fresh.snapshot().state.value == "active"
    await restore_trading_controls(session, fresh)
    assert fresh.snapshot().state.value == "emergency_stop"
    result = await RecoveryService(session).run()
    assert result["emergency_stop"] is True


@pytest.mark.asyncio
async def test_recovery_fails_stale_run(session: AsyncSession) -> None:
    session.add(
        DailyWorkflowRun(
            id=uuid4(),
            session_date="2020-01-02",
            calendar_name="NYSE",
            current_state=DailyWorkflowState.INTRADAY.value,
            status="running",
            timezone="America/New_York",
            metadata_json={},
        )
    )
    await session.flush()
    result = await RecoveryService(session).run(now=datetime(2026, 8, 3, tzinfo=UTC))
    assert any(a.startswith("fail_stale:") for a in result["actions"])


@pytest.mark.asyncio
async def test_closing_policy_no_broker() -> None:
    eng = ClosingPolicyEngine()
    decision = eng.decide(
        as_of=datetime.now(UTC),
        positions=[{"symbol": "AAPL", "quantity": 10, "is_intraday_only": True}],
        policy=ClosingPolicy.CLOSE_INTRADAY_ONLY,
        intraday_symbols={"AAPL"},
    )
    assert decision.broker_orders_allowed is False
    assert decision.plans[0].action == "close"


@pytest.mark.asyncio
async def test_scheduler_disabled_by_default() -> None:
    assert _scheduler_enabled(get_settings()) is False
    assert start_scheduler(get_settings()) is None


@pytest.mark.asyncio
async def test_scheduler_registers_universe_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.core.scheduler as sched

    await sched.stop_scheduler()
    settings = get_settings().model_copy(
        update={
            "enable_scheduler": True,
            "universe_manager_enabled": True,
            "universe_mode": "dynamic",
            "universe_refresh_seconds": 600,
        }
    )
    monkeypatch.setattr(sched, "get_settings", lambda: settings)
    try:
        assert sched._universe_refresh_enabled(settings) is True
        started = start_scheduler(settings)
        assert started is not None
        ids = {j["id"] for j in sched.upcoming_jobs()}
        assert "daily_workflow_dispatch" in ids
        assert "universe_refresh" in ids
    finally:
        await sched.stop_scheduler()


@pytest.mark.asyncio
async def test_scheduler_registers_broker_reconciliation(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.core.scheduler as sched

    await sched.stop_scheduler()
    settings = get_settings().model_copy(
        update={
            "enable_scheduler": True,
            "enable_broker_connection": True,
            "broker_reconciliation_interval_seconds": 45,
            "universe_mode": "static",
        }
    )
    monkeypatch.setattr(sched, "get_settings", lambda: settings)
    try:
        assert sched._broker_recon_enabled(settings) is True
        started = start_scheduler(settings)
        assert started is not None
        ids = {j["id"] for j in sched.upcoming_jobs()}
        assert "daily_workflow_dispatch" in ids
        assert "broker_reconciliation" in ids
    finally:
        await sched.stop_scheduler()


@pytest.mark.asyncio
async def test_broker_recon_skipped_without_broker_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.core.scheduler as sched

    settings = get_settings().model_copy(
        update={
            "enable_scheduler": True,
            "enable_broker_connection": False,
            "enable_broker_orders": False,
        }
    )
    assert sched._broker_recon_enabled(settings) is False
    await sched.stop_scheduler()
    try:
        started = start_scheduler(settings)
        assert started is not None
        ids = {j["id"] for j in sched.upcoming_jobs()}
        assert "broker_reconciliation" not in ids
    finally:
        await sched.stop_scheduler()


@pytest.mark.asyncio
async def test_universe_refresh_skipped_when_static(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.core.scheduler as sched

    settings = get_settings().model_copy(
        update={"enable_scheduler": True, "universe_mode": "static"}
    )
    assert sched._universe_refresh_enabled(settings) is False
    await sched.stop_scheduler()
    try:
        started = start_scheduler(settings)
        assert started is not None
        ids = {j["id"] for j in sched.upcoming_jobs()}
        assert "universe_refresh" not in ids
    finally:
        await sched.stop_scheduler()


def test_coalesce_keeps_latest_intraday_only() -> None:
    from app.core.scheduler import _coalesce_due_jobs

    class Job:
        def __init__(self, key: str, planned: datetime) -> None:
            self.job_key = key
            self.planned_at = planned
            self.status = "planned"
            self.error = None
            self.completed_at = None

    t0 = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)
    jobs = [
        Job("premarket_analysis", t0),
        Job("intraday_eval_0", t0 + timedelta(minutes=20)),
        Job("intraday_eval_1", t0 + timedelta(minutes=40)),
        Job("intraday_eval_2", t0 + timedelta(minutes=60)),
        Job("closing_window", t0 + timedelta(hours=6)),
    ]
    out = _coalesce_due_jobs(jobs)
    assert [j.job_key for j in out] == [
        "premarket_analysis",
        "intraday_eval_2",
        "closing_window",
    ]
    assert jobs[1].status == "skipped"
    assert jobs[2].status == "skipped"
    assert jobs[3].status == "planned"


def test_prioritize_open_venue_jobs_runs_live_tape_first() -> None:
    from app.core.config import Settings
    from app.core.scheduler import _prioritize_open_venue_jobs

    class Job:
        def __init__(self, key: str, planned: datetime) -> None:
            self.job_key = key
            self.planned_at = planned

    # 13:45 Sydney Friday = AU regular; 23:45 ET Thursday = US after hours.
    now = datetime(2026, 8, 21, 3, 45, tzinfo=UTC)
    us = Job("US:intraday_eval_179", datetime(2026, 8, 20, 19, 55, tzinfo=UTC))
    au = Job("AU:intraday_eval_26", datetime(2026, 8, 21, 3, 40, tzinfo=UTC))
    settings = Settings(app_env="test", enabled_venues=["US", "AU"], primary_venue="AU")
    out = _prioritize_open_venue_jobs([us, au], settings, now)
    assert [j.job_key for j in out] == ["AU:intraday_eval_26", "US:intraday_eval_179"]


def test_coalesce_before_limit_keeps_closing_with_dense_intraday() -> None:
    """Dispatch must coalesce then slice — not slice then coalesce."""
    from app.core.scheduler import _coalesce_due_jobs

    class Job:
        def __init__(self, key: str, planned: datetime) -> None:
            self.job_key = key
            self.planned_at = planned
            self.status = "planned"
            self.error = None
            self.completed_at = None

    t0 = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)
    # 25 overdue intraday evals + one closing later — pre-slice[:20] would drop closing.
    jobs = [Job(f"US:intraday_eval_{i}", t0 + timedelta(minutes=i)) for i in range(25)]
    closing = Job("US:closing_window", t0 + timedelta(hours=6))
    jobs.append(closing)
    coalesced = _coalesce_due_jobs(list(jobs))
    due = coalesced[:20]
    assert any(j.job_key == "US:closing_window" for j in due)
    assert sum(1 for j in due if "intraday_eval" in j.job_key) == 1


@pytest.mark.asyncio
async def test_scheduler_bootstrap_prepares_sessions(session: AsyncSession) -> None:
    from app.core import scheduler as sched_mod
    from app.market.venues import enabled_venues

    sched_mod._PREPARE_CACHE.clear()
    settings = get_settings()
    prepared = await sched_mod._ensure_sessions_prepared(session, settings)
    venues = {v.value for v in enabled_venues(settings)}
    # today + next trading day per enabled venue
    assert len(prepared) == 2 * len(venues)
    assert {p.split(":", 1)[0] for p in prepared} == venues
    # Throttled within TTL — no duplicate prepare work.
    again = await sched_mod._ensure_sessions_prepared(session, settings)
    assert again == []
    # Expired cache entries prepare again.
    for key in list(sched_mod._PREPARE_CACHE):
        sched_mod._PREPARE_CACHE[key] = datetime.now(UTC) - timedelta(
            seconds=sched_mod._PREPARE_TTL_SECONDS + 1
        )
    third = await sched_mod._ensure_sessions_prepared(session, settings)
    assert len(third) == 2 * len(venues)


@pytest.mark.asyncio
async def test_holiday_prepare_no_jobs(session: AsyncSession) -> None:
    svc = DailyWorkflowService(session, settings=get_settings())
    await svc.prepare(session_date="2026-11-26")
    jobs = await svc.planned_jobs("2026-11-26")
    assert jobs == []


@pytest.mark.asyncio
async def test_broker_flags_default_false(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.core.scheduler as sched

    called = {"broker": False}

    class Boom:
        def __getattr__(self, name: str) -> MagicMock:
            called["broker"] = True
            raise AssertionError("broker must not be touched")

    monkeypatch.setitem(__import__("sys").modules, "app.brokers.ibkr", Boom())
    assert get_settings().enable_broker_orders is False
    assert get_settings().enable_automated_execution is False
    assert called["broker"] is False
    assert sched._scheduler_enabled(get_settings()) is False
