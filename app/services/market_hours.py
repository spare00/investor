"""US equity session helpers (calendar wiring expands in Phase 2)."""

from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

US_EASTERN = ZoneInfo("America/New_York")

# Regular session (excludes holidays / early closes — use exchange_calendars later)
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
PREMARKET_OPEN = time(4, 0)


def is_weekday(dt: datetime) -> bool:
    local = dt.astimezone(US_EASTERN) if dt.tzinfo else dt.replace(tzinfo=UTC).astimezone(US_EASTERN)
    return local.weekday() < 5


def is_regular_session(dt: datetime) -> bool:
    """True during Mon–Fri 09:30–16:00 America/New_York (holiday-unaware stub)."""
    local = dt.astimezone(US_EASTERN) if dt.tzinfo else dt.replace(tzinfo=UTC).astimezone(US_EASTERN)
    if local.weekday() >= 5:
        return False
    t = local.time()
    return REGULAR_OPEN <= t < REGULAR_CLOSE


def minutes_to_close(dt: datetime) -> int | None:
    if not is_regular_session(dt):
        return None
    local = dt.astimezone(US_EASTERN)
    close = datetime.combine(local.date(), REGULAR_CLOSE, tzinfo=US_EASTERN)
    return max(0, int((close - local).total_seconds() // 60))
