"""Trading venues (US / AU) — calendar, currency, and IBKR contract hints."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from enum import StrEnum

from app.core.config import Settings, get_settings


class Venue(StrEnum):
    US = "US"
    AU = "AU"


@dataclass(frozen=True, slots=True)
class VenueSpec:
    venue: Venue
    mic: str
    calendar_code: str
    timezone: str
    currency: str
    ib_exchange: str
    # Local-market wall-clock windows (not exchange-authoritative for all products).
    premarket_start: time | None
    postmarket_end: time | None
    # Regular close used to detect early-close sessions.
    regular_close_local: time


VENUE_SPECS: dict[Venue, VenueSpec] = {
    Venue.US: VenueSpec(
        venue=Venue.US,
        mic="XNYS",
        calendar_code="XNYS",
        timezone="America/New_York",
        currency="USD",
        ib_exchange="SMART",
        premarket_start=time(4, 0),
        postmarket_end=time(20, 0),
        regular_close_local=time(16, 0),
    ),
    Venue.AU: VenueSpec(
        venue=Venue.AU,
        mic="XASX",
        calendar_code="XASX",
        timezone="Australia/Sydney",
        currency="AUD",
        # Prefer SMART routing; direct ASX may trip Gateway precautionary error 10311.
        ib_exchange="SMART",
        # ASX pre-open auction typically from ~07:00 local.
        premarket_start=time(7, 0),
        postmarket_end=time(16, 10),
        regular_close_local=time(16, 0),
    ),
}

_CALENDAR_TO_VENUE: dict[str, Venue] = {
    "NYSE": Venue.US,
    "NASDAQ": Venue.US,
    "XNYS": Venue.US,
    "XNAS": Venue.US,
    "ASX": Venue.AU,
    "XASX": Venue.AU,
}


def parse_venue(value: str | Venue | None) -> Venue | None:
    if value is None:
        return None
    if isinstance(value, Venue):
        return value
    text = str(value).strip().upper()
    if not text:
        return None
    try:
        return Venue(text)
    except ValueError as exc:
        raise ValueError(f"unknown_venue:{value}") from exc


def get_venue_spec(venue: Venue | str) -> VenueSpec:
    v = parse_venue(venue)
    if v is None:
        raise ValueError("venue_required")
    return VENUE_SPECS[v]


def resolve_venue(settings: Settings | None = None, *, venue: Venue | str | None = None) -> Venue:
    """Resolve the active venue: explicit arg → PRIMARY_VENUE → calendar mapping → US."""
    explicit = parse_venue(venue)
    if explicit is not None:
        return explicit
    cfg = settings or get_settings()
    primary = parse_venue(getattr(cfg, "primary_venue", None) or None)
    if primary is not None:
        return primary
    cal = (cfg.market_calendar or "NYSE").upper()
    return _CALENDAR_TO_VENUE.get(cal, Venue.US)


def resolve_venue_spec(
    settings: Settings | None = None, *, venue: Venue | str | None = None
) -> VenueSpec:
    return get_venue_spec(resolve_venue(settings, venue=venue))


def ib_qualify_candidates(
    settings: Settings | None = None,
    *,
    venue: Venue | str | None = None,
) -> list[tuple[str, str]]:
    """Ordered (exchange, currency) pairs for IBKR stock qualification."""
    cfg = settings or get_settings()
    preferred = resolve_venue_spec(cfg, venue=venue)
    defaults = (
        (cfg.ibkr_default_exchange or "SMART").upper(),
        (cfg.ibkr_default_currency or "USD").upper(),
    )
    ordered: list[tuple[str, str]] = [
        (preferred.ib_exchange, preferred.currency),
        defaults,
        ("SMART", "USD"),
        ("ASX", "AUD"),
        ("SMART", "AUD"),
    ]
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for pair in ordered:
        if pair in seen:
            continue
        seen.add(pair)
        out.append(pair)
    return out


def enabled_venues(settings: Settings | None = None) -> list[Venue]:
    """Venues the scheduler should prepare/dispatch (defaults to primary only)."""
    cfg = settings or get_settings()
    raw = list(getattr(cfg, "enabled_venues", None) or [])
    out: list[Venue] = []
    seen: set[Venue] = set()
    for item in raw:
        try:
            v = parse_venue(item)
        except ValueError:
            continue
        if v is None or v in seen:
            continue
        seen.add(v)
        out.append(v)
    if not out:
        out = [resolve_venue(cfg)]
    return out


def run_calendar_name(venue: Venue | str, settings: Settings | None = None) -> str:
    """Persistable calendar_name for DailyWorkflowRun uniqueness."""
    v = parse_venue(venue) or Venue.US
    if v == Venue.AU:
        return "ASX"
    cfg = settings or get_settings()
    return (cfg.market_calendar or "NYSE").upper()


def scoped_job_key(venue: Venue | str, base: str) -> str:
    """Prefix scheduled job keys so US/AU session dates can coexist."""
    v = parse_venue(venue) or Venue.US
    base = str(base).strip()
    if base.startswith(f"{v.value}:"):
        return base
    return f"{v.value}:{base}"


def parse_scoped_job_key(job_key: str) -> tuple[Venue, str]:
    """Split ``US:premarket_analysis`` → (US, premarket_analysis). Legacy unprefixed → US."""
    text = str(job_key or "")
    if ":" in text:
        head, rest = text.split(":", 1)
        try:
            return Venue(head.upper()), rest
        except ValueError:
            pass
    return Venue.US, text


def job_key_base(job_key: str) -> str:
    return parse_scoped_job_key(job_key)[1]
