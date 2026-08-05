"""Curated expansion pool beyond TRADE_ALLOWLIST for Universe Manager adds.

Not a full-market screener — a bounded, liquid candidate set the AI may promote
onto the watchlist. Seed (TRADE_ALLOWLIST) remains the bootstrap soft boundary;
candidates expand it without inventing obscure tickers.
"""

from __future__ import annotations

from app.core.config import Settings

# Liquid US names / sector ETFs commonly useful across horizons.
# Prefer mega-cap / high-ADV; Universe Manager still assigns horizon + thesis.
DEFAULT_CANDIDATE_POOL: tuple[str, ...] = (
    # Broad / sector ETFs
    "XLK",
    "XLF",
    "XLE",
    "XBI",
    "SMH",
    "SOXX",
    "ARKK",
    "IWM",
    "EEM",
    # Mega / large liquid singles
    "JPM",
    "XOM",
    "UNH",
    "V",
    "MA",
    "COST",
    "NFLX",
    "CRM",
    "ORCL",
    "INTC",
    "MU",
    "PLTR",
    "UBER",
    "SHOP",
    "BA",
    "CAT",
    "GS",
    "WMT",
    "HD",
    "DIS",
    "PYPL",
    "COIN",
    "CRWD",
    "PANW",
    "NOW",
    "SNOW",
    "AMD",
    "AVGO",
)


def curated_candidate_pool(settings: Settings | None = None) -> list[str]:
    """Configured pool if set; otherwise built-in curated list."""
    if settings is not None and settings.universe_candidate_pool:
        return [s.upper().strip() for s in settings.universe_candidate_pool if s.strip()]
    return list(DEFAULT_CANDIDATE_POOL)


def addable_universe(settings: Settings, *, known_symbols: set[str] | None = None) -> set[str]:
    """Symbols the Universe Manager may newly add (seed ∪ candidates ∪ known)."""
    seed = {s.upper() for s in settings.trade_allowlist}
    known = {s.upper() for s in (known_symbols or set())}
    if not settings.universe_allow_candidate_adds:
        return seed | known
    return seed | known | set(curated_candidate_pool(settings))
