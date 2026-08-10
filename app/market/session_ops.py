"""Resolve which venue manual ops should target (dashboard / CLI)."""

from __future__ import annotations

from datetime import datetime

from app.core.config import Settings, get_settings
from app.market.venues import Venue, enabled_venues

# Phases where a book is in its trading day cycle (not deep overnight idle).
_ACTIVE_OPS_PHASES: dict[str, int] = {
    "REGULAR": 100,
    "FORCE_CLOSE_WINDOW": 90,
    "CLOSING_WINDOW": 80,
    "PREMARKET": 70,
    "POSTMARKET": 50,
    "AFTER_HOURS": 40,
    "BEFORE_PREMARKET": 20,
}


def resolve_active_session_venue(
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
    prefer: str | None = None,
) -> Venue | None:
    """Pick the enabled venue currently in a session cycle.

    Dual-book sessions do not overlap on BNE wall-clock, so typically one match.
    When none are active, returns None (idle). Explicit ``prefer`` wins if that
    venue is enabled (ops override).
    """
    from app.market.calendar import MarketCalendarService
    from app.market.venues import parse_venue

    cfg = settings or get_settings()
    if prefer and str(prefer).strip().lower() not in {"", "auto"}:
        chosen = parse_venue(prefer)
        if chosen is not None and chosen in enabled_venues(cfg):
            return chosen
        return None

    ranked: list[tuple[int, Venue]] = []
    for venue in enabled_venues(cfg):
        phase = MarketCalendarService(cfg, venue=venue).get_market_status(now).phase
        score = _ACTIVE_OPS_PHASES.get(phase)
        if score is None:
            continue
        ranked.append((score, venue))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (-item[0], item[1].value))
    return ranked[0][1]


def active_session_summary(
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Dashboard / API snapshot for manual ops venue targeting."""
    from app.market.calendar import MarketCalendarService

    cfg = settings or get_settings()
    phases: dict[str, str] = {}
    for venue in enabled_venues(cfg):
        phases[venue.value] = MarketCalendarService(cfg, venue=venue).get_market_status(now).phase
    active = resolve_active_session_venue(cfg, now=now)
    return {
        "active_ops_venue": active.value if active else None,
        "venue_phases": phases,
        "pause_and_emergency_global": True,
    }
