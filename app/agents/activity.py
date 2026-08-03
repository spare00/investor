"""In-process agent activity lamps — who is running right now / last finish."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any

_lock = threading.Lock()
# agent_name -> {state, started_at, finished_at, last_error, run_id}
_activity: dict[str, dict[str, Any]] = {}

AGENT_ORDER = (
    "market_intelligence",
    "macro_strategist",
    "quant_strategist",
    "risk_manager",
    "devils_advocate",
    "cio",
)

AGENT_SHORT = {
    "market_intelligence": "MI",
    "macro_strategist": "Macro",
    "quant_strategist": "Quant",
    "risk_manager": "Risk",
    "devils_advocate": "Devil",
    "cio": "CIO",
}


def mark_agent_started(agent_name: str, *, run_id: str | None = None) -> None:
    now = datetime.now(UTC).isoformat()
    with _lock:
        _activity[agent_name] = {
            "state": "running",
            "started_at": now,
            "finished_at": None,
            "last_error": None,
            "run_id": run_id,
            "outcome": None,
        }


def mark_agent_finished(
    agent_name: str,
    *,
    outcome: str = "completed",
    error: str | None = None,
) -> None:
    now = datetime.now(UTC).isoformat()
    with _lock:
        prev = dict(_activity.get(agent_name) or {})
        prev.update(
            {
                "state": "idle" if outcome in {"completed", "fallback"} else "failed",
                "finished_at": now,
                "last_error": (error or "")[:240] or None,
                "outcome": outcome,
            }
        )
        if "started_at" not in prev:
            prev["started_at"] = now
        _activity[agent_name] = prev


def reset_agent_activity_for_tests() -> None:
    with _lock:
        _activity.clear()


def snapshot_agent_activity() -> dict[str, dict[str, Any]]:
    with _lock:
        return {k: dict(v) for k, v in _activity.items()}


def classify_agent_lamp(
    *,
    live: dict[str, Any] | None,
    last_run_status: str | None,
    last_started_at: datetime | None,
    now: datetime | None = None,
    recent_seconds: int = 45 * 60,
    stale_seconds: int = 6 * 60 * 60,
) -> dict[str, Any]:
    """
    Derive a dashboard lamp:
      running | ready | stale | failed | silent
    """
    now = now or datetime.now(UTC)
    if live and live.get("state") == "running":
        return {
            "lamp": "running",
            "label": "active",
            "detail": "running now",
            "live": True,
        }
    if live and live.get("state") == "failed":
        return {
            "lamp": "failed",
            "label": "failed",
            "detail": live.get("last_error") or "last run failed",
            "live": False,
        }

    status = (last_run_status or "").lower()
    if status in {"failed", "error"}:
        return {"lamp": "failed", "label": "failed", "detail": "last persisted run failed", "live": False}

    if last_started_at is None and not live:
        return {"lamp": "silent", "label": "inactive", "detail": "no runs yet", "live": False}

    ts = last_started_at
    if ts is not None and ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    # Prefer live finished_at when newer
    if live and live.get("finished_at"):
        try:
            finished = datetime.fromisoformat(str(live["finished_at"]))
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=UTC)
            if ts is None or finished >= ts:
                ts = finished
                if live.get("outcome") == "fallback":
                    return {
                        "lamp": "ready",
                        "label": "fallback",
                        "detail": "completed via fallback",
                        "live": False,
                    }
        except ValueError:
            pass

    if ts is None:
        return {"lamp": "silent", "label": "inactive", "detail": "no timestamp", "live": False}

    age = (now - ts).total_seconds()
    if age <= recent_seconds:
        return {
            "lamp": "ready",
            "label": "ready",
            "detail": f"last run {int(age)}s ago",
            "live": False,
        }
    if age <= stale_seconds:
        return {
            "lamp": "stale",
            "label": "idle",
            "detail": f"last run {int(age // 60)}m ago",
            "live": False,
        }
    return {
        "lamp": "silent",
        "label": "inactive",
        "detail": f"last run {int(age // 3600)}h ago",
        "live": False,
    }
