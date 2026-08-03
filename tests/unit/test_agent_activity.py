"""Agent activity lamp classification tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.agents.activity import (
    classify_agent_lamp,
    mark_agent_finished,
    mark_agent_started,
    reset_agent_activity_for_tests,
    snapshot_agent_activity,
)


def setup_function() -> None:
    reset_agent_activity_for_tests()


def teardown_function() -> None:
    reset_agent_activity_for_tests()


def test_live_running_wins() -> None:
    mark_agent_started("cio", run_id="1")
    lamp = classify_agent_lamp(
        live=snapshot_agent_activity()["cio"],
        last_run_status="completed",
        last_started_at=datetime.now(UTC) - timedelta(hours=2),
    )
    assert lamp["lamp"] == "running"
    assert lamp["live"] is True


def test_recent_completed_is_ready() -> None:
    now = datetime.now(UTC)
    lamp = classify_agent_lamp(
        live=None,
        last_run_status="completed",
        last_started_at=now - timedelta(minutes=10),
        now=now,
    )
    assert lamp["lamp"] == "ready"


def test_old_run_is_silent() -> None:
    now = datetime.now(UTC)
    lamp = classify_agent_lamp(
        live=None,
        last_run_status="completed",
        last_started_at=now - timedelta(hours=10),
        now=now,
    )
    assert lamp["lamp"] == "silent"


def test_failed_live_state() -> None:
    mark_agent_started("risk_manager")
    mark_agent_finished("risk_manager", outcome="failed", error="boom")
    lamp = classify_agent_lamp(
        live=snapshot_agent_activity()["risk_manager"],
        last_run_status="completed",
        last_started_at=datetime.now(UTC),
    )
    assert lamp["lamp"] == "failed"
