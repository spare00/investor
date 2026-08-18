"""Watch local committee wall time vs the scheduler job cap as the book grows."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def _as_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def job_duration_seconds(
    row: dict[str, Any],
    *,
    now: datetime | None = None,
) -> float | None:
    """Wall time for a session job. Running jobs count from started_at to now."""
    start = _as_dt(row.get("started_at"))
    if start is None:
        return None
    end = _as_dt(row.get("completed_at"))
    if end is None:
        status = str(row.get("status") or "").lower()
        if status == "running":
            end = now or datetime.now(UTC)
        else:
            return None
    return max(0.0, (end - start).total_seconds())


def _is_intraday_eval(row: dict[str, Any]) -> bool:
    key = str(row.get("job_key") or "")
    return "intraday_eval" in key or str(row.get("job_type") or "") == "intraday_eval"


def _is_timeout(row: dict[str, Any]) -> bool:
    err = str(row.get("error") or "")
    return err.startswith("job_action_timeout") or "job_action_timeout" in err


def build_committee_watch(
    session_jobs: list[dict[str, Any]],
    *,
    timeout_cap_seconds: int,
    watchlist_symbols: int,
    focus_symbols: int,
    allowlist_symbols: int,
    llm_is_local: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Operator snapshot: last eval seconds vs 8-minute cap, timeouts, book size."""
    cap = max(1, int(timeout_cap_seconds))
    current = now or datetime.now(UTC)
    evals: list[dict[str, Any]] = []
    for raw in session_jobs or []:
        if not _is_intraday_eval(raw):
            continue
        row = dict(raw)
        dur = job_duration_seconds(row, now=current)
        row["duration_s"] = round(dur, 1) if dur is not None else None
        evals.append(row)

    finished = [
        j
        for j in evals
        if str(j.get("status") or "").lower() in {"completed", "failed", "skipped"}
        and j.get("duration_s") is not None
    ]
    finished.sort(key=lambda j: str(j.get("completed_at") or j.get("started_at") or ""))
    recent = finished[-8:]
    last = recent[-1] if recent else None
    last_s = float(last["duration_s"]) if last and last.get("duration_s") is not None else None
    timeout_n = sum(1 for j in evals if _is_timeout(j))
    done_n = len([j for j in evals if str(j.get("status") or "").lower() in {"completed", "failed", "skipped"}])
    headroom_pct: float | None = None
    if last_s is not None:
        headroom_pct = max(0.0, round((1.0 - last_s / cap) * 100.0, 1))

    used_frac = (last_s / cap) if last_s is not None else 0.0
    if timeout_n > 0 or used_frac >= 0.9:
        level = "bad"
    elif used_frac >= 0.7:
        level = "warn"
    else:
        level = "ok"

    detail = "no finished evals this session"
    if last_s is not None:
        detail = (
            f"last eval {int(round(last_s))}s / {cap}s cap "
            f"({headroom_pct:.0f}% headroom) · "
            f"watch {watchlist_symbols} focus {focus_symbols}"
        )
        if timeout_n:
            detail += f" · {timeout_n} timeout(s)"

    return {
        "llm_is_local": bool(llm_is_local),
        "timeout_cap_seconds": cap,
        "watchlist_symbols": int(watchlist_symbols),
        "focus_symbols": int(focus_symbols),
        "allowlist_symbols": int(allowlist_symbols),
        "last_eval_seconds": last_s,
        "last_eval_job": (last or {}).get("job_key"),
        "last_eval_status": (last or {}).get("status"),
        "headroom_pct": headroom_pct,
        "timeouts_session": timeout_n,
        "evals_done_session": done_n,
        "level": level,
        "detail": detail,
        "recent": [
            {
                "job_key": j.get("job_key"),
                "status": j.get("status"),
                "duration_s": j.get("duration_s"),
                "timeout": _is_timeout(j),
            }
            for j in recent
        ],
    }
