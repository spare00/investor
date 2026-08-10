"""Trading venues (US / AU) — calendar, currency, and IBKR contract hints."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from enum import StrEnum
from typing import Any

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


def venue_for_symbol(
    symbol: str,
    settings: Settings | None = None,
    *,
    exchange: str | None = None,
    currency: str | None = None,
    venue: Venue | str | None = None,
) -> Venue:
    """Best-effort symbol → venue: explicit → exchange/currency → allowlist → primary."""
    explicit = parse_venue(venue)
    if explicit is not None:
        return explicit

    ex = (exchange or "").upper()
    ccy = (currency or "").upper()
    if ex in {"ASX", "XASX"} or ccy == "AUD":
        return Venue.AU
    if ex in {"NASDAQ", "NYSE", "ARCA", "AMEX", "BATS", "IEX"} or ccy == "USD":
        # SMART alone is ambiguous (AU also qualifies via SMART/AUD).
        if ex and ex != "SMART":
            return Venue.US

    cfg = settings or get_settings()
    sym = (symbol or "").upper().strip()
    if sym and sym in cfg.allowlist_for_venue(Venue.AU):
        # Prefer AU when listed only there; if also on US list, keep primary.
        if sym not in cfg.allowlist_for_venue(Venue.US):
            return Venue.AU
    if sym and sym in cfg.allowlist_for_venue(Venue.US):
        if sym not in cfg.allowlist_for_venue(Venue.AU):
            return Venue.US
    return resolve_venue(cfg)


def combined_entry_allowlist(settings: Settings | None = None) -> set[str]:
    """Union of allowlists for all enabled venues (risk gate for mixed books)."""
    cfg = settings or get_settings()
    out: set[str] = set()
    for v in enabled_venues(cfg):
        out |= cfg.allowlist_for_venue(v)
    if not out:
        out = cfg.allowlist_set()
    return out


def position_venue(
    *,
    symbol: str | None = None,
    venue: Venue | str | None = None,
    exchange: str | None = None,
    currency: str | None = None,
    settings: Settings | None = None,
) -> Venue:
    """Resolve venue for a position-like row (explicit column preferred)."""
    return venue_for_symbol(
        symbol or "",
        settings,
        exchange=exchange,
        currency=currency,
        venue=venue,
    )


def holdings_for_venue(
    positions: list[Any],
    venue: Venue | str,
    settings: Settings | None = None,
) -> list[str]:
    """Symbols from open positions that belong to ``venue`` (order-preserving unique)."""
    want = parse_venue(venue) or Venue.US
    cfg = settings or get_settings()
    out: list[str] = []
    seen: set[str] = set()
    for raw in positions:
        if isinstance(raw, dict):
            symbol = str(raw.get("symbol") or "").upper()
            qty = float(
                raw.get("quantity")
                if raw.get("quantity") is not None
                else raw.get("qty")
                or 0
            )
            row_venue = raw.get("venue")
            exchange = raw.get("exchange")
            currency = raw.get("currency")
        else:
            symbol = str(getattr(raw, "symbol", "") or "").upper()
            qty = float(getattr(raw, "quantity", None) or getattr(raw, "qty", None) or 0)
            row_venue = getattr(raw, "venue", None)
            exchange = getattr(raw, "exchange", None)
            currency = getattr(raw, "currency", None)
        if not symbol or abs(qty) < 1e-12:
            continue
        resolved = position_venue(
            symbol=symbol,
            venue=row_venue,
            exchange=str(exchange) if exchange else None,
            currency=str(currency) if currency else None,
            settings=cfg,
        )
        if resolved != want or symbol in seen:
            continue
        seen.add(symbol)
        out.append(symbol)
    return out


def news_relevant_to_venue(
    news_symbols: list[str] | None,
    venue: Venue | str,
    *,
    settings: Settings | None = None,
    held_symbols: set[str] | None = None,
) -> bool:
    """True if news should escalate for this venue book.

    Macro / untagged items (empty symbols) stay relevant to every book.
    Tagged items must intersect the venue allowlist or that book's holdings.
    """
    want = parse_venue(venue) or Venue.US
    cfg = settings or get_settings()
    tagged = {str(s).upper() for s in (news_symbols or []) if s}
    if not tagged:
        return True
    allow = set(cfg.allowlist_for_venue(want))
    held = {str(s).upper() for s in (held_symbols or []) if s}
    if want == Venue.AU:
        allow |= {(cfg.primary_benchmark_au or "VAS").upper()}
    else:
        allow |= {(cfg.primary_benchmark or "SPY").upper()}
    return bool(tagged & (allow | held))
