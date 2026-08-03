"""Timezone helpers — store UTC, display US Eastern and Brisbane."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.core.config import get_settings


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def format_display(dt: datetime, tz_name: str | None = None) -> str:
    settings = get_settings()
    zone = ZoneInfo(tz_name or settings.display_tz_us)
    local = to_utc(dt).astimezone(zone)
    return local.isoformat()


def format_us_eastern(dt: datetime) -> str:
    return format_display(dt, get_settings().display_tz_us)


def format_brisbane(dt: datetime) -> str:
    return format_display(dt, get_settings().display_tz_local)


def dual_timezone_labels(dt: datetime) -> dict[str, str]:
    return {
        "utc": to_utc(dt).isoformat(),
        "us_eastern": format_us_eastern(dt),
        "brisbane": format_brisbane(dt),
    }
