"""Active venue-book context for agent runs (US or AU session).

One app manages both books around the clock; each agent invocation is scoped to
the book whose session is being worked. Roles stay identical — only the target
market changes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any

from app.core.config import Settings, get_settings
from app.market.venues import Venue, get_venue_spec, parse_venue, resolve_venue


@dataclass(frozen=True, slots=True)
class VenueBookContext:
    venue: str
    mic: str
    currency: str
    market_timezone: str
    operator_timezone: str
    session_date: str | None
    phase: str | None
    benchmark: str
    ib_exchange: str
    allowlist: tuple[str, ...]
    index_symbols: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def prompt_block(self) -> str:
        """Compact block prepended to agent user prompts."""
        allow = ", ".join(self.allowlist[:24]) or "(empty)"
        indexes = ", ".join(self.index_symbols) or "(none)"
        return (
            f"BOOK {self.venue} {self.currency} {self.phase or 'n/a'} "
            f"bench={self.benchmark} allow={allow} idx={indexes}. "
            "This book only for new entries."
        )


def index_symbols_for_venue(venue: Venue | str, settings: Settings | None = None) -> tuple[str, ...]:
    cfg = settings or get_settings()
    v = parse_venue(venue) or Venue.US
    if v == Venue.AU:
        bench = (cfg.primary_benchmark_au or "VAS").upper()
        extras = ["IOZ", "NDQ"]
        out = [bench]
        for s in extras:
            if s != bench:
                out.append(s)
        return tuple(out)
    return ("SPY", "QQQ", "IWM", "DIA")


def build_venue_book_context(
    settings: Settings | None = None,
    *,
    venue: Venue | str | None = None,
    session_date: str | date | None = None,
    phase: str | None = None,
    allowlist: list[str] | set[str] | None = None,
) -> VenueBookContext:
    cfg = settings or get_settings()
    book = resolve_venue(cfg, venue=venue)
    spec = get_venue_spec(book)
    if allowlist is None:
        allow = sorted(cfg.allowlist_for_venue(book))
    else:
        allow = sorted({str(s).upper() for s in allowlist if s})
    if book == Venue.AU:
        bench = (cfg.primary_benchmark_au or "VAS").upper()
    else:
        bench = (cfg.primary_benchmark or "SPY").upper()
    day: str | None
    if session_date is None:
        day = None
    elif isinstance(session_date, date):
        day = session_date.isoformat()
    else:
        day = str(session_date)
    return VenueBookContext(
        venue=book.value,
        mic=spec.mic,
        currency=spec.currency,
        market_timezone=spec.timezone,
        operator_timezone=cfg.operator_timezone or "Australia/Brisbane",
        session_date=day,
        phase=phase,
        benchmark=bench,
        ib_exchange=spec.ib_exchange,
        allowlist=tuple(allow),
        index_symbols=index_symbols_for_venue(book, cfg),
    )


def book_from_mapping(raw: dict[str, Any] | None) -> VenueBookContext | None:
    if not raw:
        return None
    try:
        return VenueBookContext(
            venue=str(raw.get("venue") or "US"),
            mic=str(raw.get("mic") or ""),
            currency=str(raw.get("currency") or "USD"),
            market_timezone=str(raw.get("market_timezone") or ""),
            operator_timezone=str(raw.get("operator_timezone") or "Australia/Brisbane"),
            session_date=str(raw["session_date"]) if raw.get("session_date") else None,
            phase=str(raw["phase"]) if raw.get("phase") else None,
            benchmark=str(raw.get("benchmark") or "SPY"),
            ib_exchange=str(raw.get("ib_exchange") or "SMART"),
            allowlist=tuple(raw.get("allowlist") or ()),
            index_symbols=tuple(raw.get("index_symbols") or ()),
        )
    except (TypeError, ValueError, KeyError):
        return None
