"""Universe refresh scheduling helpers (weekend / session gates)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.config import Settings


def is_operator_weekend(cfg: Settings, now: datetime | None = None) -> bool:
    """Saturday/Sunday in operator_timezone (default Australia/Brisbane)."""
    from zoneinfo import ZoneInfo

    tz_name = (cfg.operator_timezone or cfg.display_tz_local or "Australia/Brisbane").strip()
    local = (now or datetime.now(UTC)).astimezone(ZoneInfo(tz_name))
    return local.weekday() >= 5  # Sat=5, Sun=6
