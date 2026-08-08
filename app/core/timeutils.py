"""Timezone helpers — store UTC, display US Eastern and Brisbane."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.core.config import Settings, get_settings


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def operator_zone(settings: Settings | None = None) -> ZoneInfo:
    cfg = settings or get_settings()
    return ZoneInfo(cfg.operator_timezone or cfg.display_tz_local or "Australia/Brisbane")


def operator_now(settings: Settings | None = None, *, now: datetime | None = None) -> datetime:
    """Wall clock in operator TZ (default Brisbane); ``now`` must be timezone-aware UTC-ish."""
    instant = to_utc(now) if now is not None else utc_now()
    return instant.astimezone(operator_zone(settings))


def operator_calendar_day(
    settings: Settings | None = None, *, now: datetime | None = None
) -> date:
    return operator_now(settings, now=now).date()


def operator_calendar_day_iso(
    settings: Settings | None = None, *, now: datetime | None = None
) -> str:
    return operator_calendar_day(settings, now=now).isoformat()


def operator_calendar_month(
    settings: Settings | None = None, *, now: datetime | None = None
) -> str:
    return operator_now(settings, now=now).strftime("%Y-%m")


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
