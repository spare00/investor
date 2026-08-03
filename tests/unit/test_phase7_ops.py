"""Phase 7 operational module tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.alerts.base import AlertSeverity, AlertStatus
from app.alerts.fake_provider import FakeAlertProvider
from app.alerts.service import AlertService
from app.core.config import AppEnv, Settings, clear_settings_cache
from app.core.database import Base
from app.core.metrics import (
    AGENT_RUNS,
    WORKFLOW_FAILURES,
    metrics_payload,
)
from app.ops.backup import BackupService
from app.ops.fault_injection import FaultInjectionError, FaultInjectionFramework, FaultKind
from app.ops.readiness import GateEvaluator, ReadinessGate
from app.ops.retention import RetentionPolicy
from app.simulation.runner import MultiDaySimulationRunner, SimulationScenario


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


@pytest.fixture
def settings() -> Settings:
    clear_settings_cache()
    return Settings(
        enable_alerts=True,
        alert_provider="fake",
        critical_alert_cooldown_seconds=60,
        warning_alert_cooldown_seconds=300,
        enable_fault_injection=True,
        app_env=AppEnv.TEST,
    )


@pytest.mark.asyncio
async def test_alert_service_emit_dedup_and_cooldown(settings: Settings) -> None:
    provider = FakeAlertProvider()
    svc = AlertService(settings=settings, provider=provider)

    first = await svc.emit(
        code="test.alert",
        message="hello",
        severity=AlertSeverity.INFO,
    )
    assert first.emitted is True
    assert len(provider.sent) == 1

    dup = await svc.emit(
        code="test.alert",
        message="hello again",
        severity=AlertSeverity.INFO,
    )
    assert dup.emitted is False
    assert dup.reason == "deduplicated"

    cooldown = await svc.emit(
        code="other.alert",
        message="critical",
        severity=AlertSeverity.CRITICAL,
    )
    assert cooldown.emitted is True
    cooled = await svc.emit(
        code="other.alert",
        message="critical again",
        severity=AlertSeverity.CRITICAL,
    )
    assert cooled.emitted is False
    assert cooled.reason == "cooldown"

    ack = await svc.acknowledge(first.alert_id)  # type: ignore[arg-type]
    assert ack.emitted is True
    resolved = await svc.resolve(first.alert_id)  # type: ignore[arg-type]
    assert resolved.emitted is True

    listed = await svc.list_alerts(status=AlertStatus.RESOLVED)
    assert len(listed) == 1


def test_fault_injection_gated(settings: Settings) -> None:
    fw = FaultInjectionFramework(settings=settings)
    fw.inject(FaultKind.PROVIDER_OUTAGE)
    assert fw.check(FaultKind.PROVIDER_OUTAGE) is True
    fw.clear(FaultKind.PROVIDER_OUTAGE)
    assert fw.check(FaultKind.PROVIDER_OUTAGE) is False

    prod = Settings(app_env=AppEnv.PRODUCTION, enable_fault_injection=True)
    prod_fw = FaultInjectionFramework(settings=prod)
    with pytest.raises(FaultInjectionError):
        prod_fw.inject(FaultKind.BROKER_OUTAGE)


def test_readiness_gate_blocks_live(settings: Settings) -> None:
    result = GateEvaluator(settings=settings).evaluate(ReadinessGate.PAPER_OBSERVE_READY)
    assert result["live_trading_allowed"] is False
    assert result["auto_promote"] is False
    assert result["current_gate"] == "PAPER_OBSERVE_READY"
    assert any(c["name"] == "live_trading_disabled" for c in result["checks"])


@pytest.mark.asyncio
async def test_backup_create_verify(session: AsyncSession, tmp_path: Path) -> None:
    svc = BackupService(session=session, root=tmp_path)
    created = await svc.create(as_zip=True)
    assert Path(created.path).exists()
    verified = svc.verify(created.path)
    assert verified.valid is True


def test_retention_dry_run(settings: Settings) -> None:
    plan = RetentionPolicy(settings=settings).plan(dry_run=True)
    assert plan.dry_run is True
    assert plan.would_delete is False
    audit = next(t for t in plan.targets if t.category == "audit_log")
    assert audit.action == "plan_only"


@pytest.mark.asyncio
async def test_simulation_deterministic(settings: Settings) -> None:
    runner_a = MultiDaySimulationRunner(settings=settings, seed=99)
    runner_b = MultiDaySimulationRunner(settings=settings, seed=99)
    a = await runner_a.run(SimulationScenario.BULL_MARKET, days=5)
    b = await runner_b.run(SimulationScenario.BULL_MARKET, days=5)
    assert isinstance(a, dict)
    assert a["ending_equity"] == b["ending_equity"]
    assert a["trades_count"] == b["trades_count"]


def test_phase7_metrics_registered() -> None:
    WORKFLOW_FAILURES.labels(kind="daily").inc()
    AGENT_RUNS.labels(agent="cio", outcome="ok").inc()
    body, _ = metrics_payload()
    assert b"investor_workflow_failures_total" in body
    assert b"investor_agent_runs_total" in body
    assert b"investor_event_queue_depth" in body
