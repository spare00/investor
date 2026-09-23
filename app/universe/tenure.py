"""Watchlist / focus consecutive-listing helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.core.timeutils import utc_now


def parse_dt(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
    text = str(raw).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def inclusive_calendar_days(since: datetime | None, *, now: datetime | None = None) -> int:
    """Calendar days from ``since`` through ``now`` (inclusive). 0 if unknown."""
    start = parse_dt(since)
    if start is None:
        return 0
    end = now or utc_now()
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    return max(0, (end.date() - start.astimezone(UTC).date()).days + 1)


def listed_since(
    payload: dict[str, Any] | None,
    created_at: datetime | None,
    *,
    active: bool,
) -> datetime | None:
    """Start of the current active listing streak, or None if not active."""
    if not active:
        return None
    stamped = parse_dt((payload or {}).get("active_since"))
    return stamped or parse_dt(created_at)


def stamp_active_since(
    payload: dict[str, Any] | None,
    *,
    now: datetime,
    reset: bool,
) -> dict[str, Any]:
    out = dict(payload or {})
    if reset or not parse_dt(out.get("active_since")):
        out["active_since"] = now.isoformat()
    out.pop("paused_at", None)
    return out


def clear_active_since(payload: dict[str, Any] | None, *, now: datetime) -> dict[str, Any]:
    out = dict(payload or {})
    out.pop("active_since", None)
    out["paused_at"] = now.isoformat()
    return out


def consecutive_focus_sessions(
    dates_newest_first: list[tuple[str, set[str]]],
) -> dict[str, int]:
    """Count unique session dates a symbol has been in focus without a gap (from latest)."""
    if not dates_newest_first:
        return {}
    current = set(dates_newest_first[0][1])
    out: dict[str, int] = {s: 0 for s in current}
    for _day, names in dates_newest_first:
        current &= names
        if not current:
            break
        for sym in current:
            out[sym] = out.get(sym, 0) + 1
    return {k: v for k, v in out.items() if v}


def churn_summary(
    roster: list[dict[str, Any]],
    *,
    stale_days: int = 30,
    new_days: int = 7,
    focus_unique_30d: int = 0,
) -> dict[str, Any]:
    active = [r for r in roster if r.get("status") == "active"]
    days = sorted(int(r.get("consecutive_listed_days") or 0) for r in active)
    n = len(days)
    median = days[n // 2] if n else 0
    pool_only = sum(1 for r in roster if r.get("status") == "pool")
    focus = sum(1 for r in roster if r.get("in_focus"))
    return {
        "active": n,
        "pool_only": pool_only,
        "median_listed_days": median,
        "stale_listed": sum(1 for d in days if d >= stale_days),
        "new_listed": sum(1 for d in days if 0 < d <= new_days),
        "focus": focus,
        "focus_unique_30d": int(focus_unique_30d),
        "stale_days": stale_days,
        "new_days": new_days,
    }
