"""Committee wall-time watch vs the scheduler job cap."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.ops.committee_watch import build_committee_watch, job_duration_seconds


def test_job_duration_seconds_completed() -> None:
    start = datetime(2026, 8, 18, 16, 0, tzinfo=UTC)
    row = {
        "started_at": start.isoformat(),
        "completed_at": (start + timedelta(seconds=125)).isoformat(),
        "status": "completed",
    }
    assert job_duration_seconds(row) == 125.0


def test_job_duration_running_uses_now() -> None:
    start = datetime(2026, 8, 18, 16, 0, tzinfo=UTC)
    now = start + timedelta(seconds=40)
    row = {"started_at": start.isoformat(), "completed_at": None, "status": "running"}
    assert job_duration_seconds(row, now=now) == 40.0


def test_committee_watch_warns_when_eval_eats_the_cap() -> None:
    start = datetime(2026, 8, 18, 16, 0, tzinfo=UTC)
    jobs = [
        {
            "job_key": "US:intraday_eval_6",
            "status": "completed",
            "started_at": start.isoformat(),
            "completed_at": (start + timedelta(seconds=360)).isoformat(),
        },
        {
            "job_key": "US:intraday_eval_7",
            "status": "failed",
            "error": "job_action_timeout:480s",
            "started_at": (start + timedelta(minutes=20)).isoformat(),
            "completed_at": (start + timedelta(minutes=20, seconds=480)).isoformat(),
        },
    ]
    watch = build_committee_watch(
        jobs,
        timeout_cap_seconds=480,
        watchlist_symbols=24,
        focus_symbols=12,
        allowlist_symbols=8,
        llm_is_local=True,
    )
    assert watch["llm_is_local"] is True
    assert watch["timeouts_session"] == 1
    assert watch["level"] == "bad"
    assert watch["last_eval_seconds"] == 480.0
    assert watch["headroom_pct"] == 0.0
    assert watch["watchlist_symbols"] == 24


def test_committee_watch_warns_at_seventy_percent_of_cap() -> None:
    start = datetime(2026, 8, 18, 16, 0, tzinfo=UTC)
    jobs = [
        {
            "job_key": "US:intraday_eval_4",
            "status": "completed",
            "started_at": start.isoformat(),
            "completed_at": (start + timedelta(seconds=336)).isoformat(),
        }
    ]
    watch = build_committee_watch(
        jobs,
        timeout_cap_seconds=480,
        watchlist_symbols=18,
        focus_symbols=10,
        allowlist_symbols=8,
        llm_is_local=True,
    )
    assert watch["level"] == "warn"
    assert watch["timeouts_session"] == 0
    assert watch["headroom_pct"] == 30.0


def test_committee_watch_ok_when_evals_are_short() -> None:
    start = datetime(2026, 8, 18, 16, 0, tzinfo=UTC)
    jobs = [
        {
            "job_key": "US:intraday_eval_5",
            "status": "completed",
            "started_at": start.isoformat(),
            "completed_at": (start + timedelta(seconds=90)).isoformat(),
        }
    ]
    watch = build_committee_watch(
        jobs,
        timeout_cap_seconds=480,
        watchlist_symbols=12,
        focus_symbols=8,
        allowlist_symbols=8,
        llm_is_local=True,
    )
    assert watch["level"] == "ok"
    assert watch["timeouts_session"] == 0
    assert watch["headroom_pct"] == 81.2
