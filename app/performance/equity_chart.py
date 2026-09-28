"""Equity-curve window and display downsample. No I/O."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

EQUITY_CHART_PERIODS: dict[str, int | None] = {
    "1w": 7,
    "1m": 31,
    "3m": 92,
    "6m": 183,
    "ytd": None,
    "1y": 366,
    "all": None,
}


def chart_window(
    period: str,
    now: datetime,
    *,
    timezone_name: str = "Australia/Brisbane",
) -> tuple[datetime, datetime]:
    """Inclusive UTC window for a chart period key. Unknown keys raise ValueError."""
    key = str(period or "").strip().lower()
    if key not in EQUITY_CHART_PERIODS:
        raise ValueError(f"unknown_equity_period:{key}")
    end = now if now.tzinfo else now.replace(tzinfo=UTC)
    end = end.astimezone(UTC)
    try:
        tz = ZoneInfo(timezone_name)
    except Exception:  # noqa: BLE001 — bad operator TZ falls back to UTC year bounds
        tz = UTC
    local_end = end.astimezone(tz)
    if key == "all":
        start_local = datetime(2000, 1, 1, tzinfo=tz)
    elif key == "ytd":
        start_local = datetime(local_end.year, 1, 1, tzinfo=tz)
    else:
        days = int(EQUITY_CHART_PERIODS[key] or 0)
        start_local = local_end - timedelta(days=days)
    return start_local.astimezone(UTC), end


def downsample_equity(
    points: list[tuple[datetime, float]],
    *,
    max_points: int = 240,
) -> list[tuple[datetime, float]]:
    """Keep chronological samples, including the first and last marks."""
    clean = [(t, float(v)) for t, v in points]
    cap = max(2, int(max_points))
    if len(clean) <= cap:
        return clean
    step = (len(clean) - 1) / (cap - 1)
    out: list[tuple[datetime, float]] = []
    last_idx = -1
    for i in range(cap):
        idx = int(round(i * step))
        if idx <= last_idx:
            continue
        if idx >= len(clean):
            idx = len(clean) - 1
        out.append(clean[idx])
        last_idx = idx
    if out[-1][0] != clean[-1][0]:
        out[-1] = clean[-1]
    return out
