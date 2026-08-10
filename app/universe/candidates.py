"""Curated expansion pool beyond TRADE_ALLOWLIST for Universe Manager adds.

Not a full-market screener — a bounded, liquid candidate set the AI may promote
onto the watchlist. Seed (TRADE_ALLOWLIST / TRADE_ALLOWLIST_AU) remains the
bootstrap soft boundary; candidates expand it without inventing obscure tickers.

Optional theme / regime ranking reorders the pool so focus-adjacent names float up.
"""

from __future__ import annotations

from app.core.config import Settings

# Liquid US names / sector ETFs commonly useful across horizons.
DEFAULT_CANDIDATE_POOL_US: tuple[str, ...] = (
    "XLK",
    "XLF",
    "XLE",
    "XBI",
    "SMH",
    "SOXX",
    "ARKK",
    "IWM",
    "EEM",
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

# Liquid ASX large-caps / sector proxies the AU book may promote.
DEFAULT_CANDIDATE_POOL_AU: tuple[str, ...] = (
    "WBC",
    "NAB",
    "ANZ",
    "MQG",
    "CSL",
    "WES",
    "WOW",
    "TLS",
    "RIO",
    "FMG",
    "GMG",
    "TCL",
    "ALL",
    "QAN",
    "STO",
    "ORG",
    "WDS",
    "MIN",
    "JHX",
    "XRO",
    "REA",
    "COL",
    "SUN",
    "QBE",
)

# Back-compat alias (US-only curated list).
DEFAULT_CANDIDATE_POOL: tuple[str, ...] = DEFAULT_CANDIDATE_POOL_US

# Theme tag → symbols to boost (must still be in candidate / seed to be addable).
THEME_SYMBOLS: dict[str, tuple[str, ...]] = {
    "tech": ("XLK", "SMH", "SOXX", "NVDA", "AMD", "AVGO", "AAPL", "MSFT", "GOOGL", "META", "CRM", "ORCL", "NOW", "SNOW", "XRO"),
    "semiconductor": ("SMH", "SOXX", "NVDA", "AMD", "AVGO", "MU", "INTC"),
    "ai": ("NVDA", "MSFT", "GOOGL", "META", "PLTR", "AMD", "AVGO", "SMH", "CRWD", "XRO"),
    "finance": ("XLF", "JPM", "GS", "V", "MA", "PYPL", "COIN", "WBC", "NAB", "ANZ", "MQG", "CBA", "QBE", "SUN"),
    "energy": ("XLE", "XOM", "STO", "ORG", "WDS"),
    "biotech": ("XBI", "UNH", "CSL"),
    "consumer": ("COST", "WMT", "HD", "DIS", "NFLX", "UBER", "SHOP", "WES", "WOW", "COL"),
    "cyber": ("CRWD", "PANW"),
    "growth": ("ARKK", "TSLA", "SHOP", "COIN", "PLTR", "NFLX", "XRO", "REA"),
    "resources": ("BHP", "RIO", "FMG", "MIN", "XLE", "XOM"),
    "asx_banks": ("CBA", "WBC", "NAB", "ANZ", "MQG"),
    "asx_etf": ("VAS", "IOZ", "NDQ", "JPEQ"),
    "risk_on": ("QQQ", "XLK", "SMH", "IWM", "ARKK", "NVDA", "TSLA", "NDQ", "XRO"),
    "risk_off": ("SPY", "DIA", "XLU", "TLT", "GLD", "JPM", "XOM", "WMT", "VAS", "IOZ", "CBA"),
    "deflation": ("TLT", "GLD", "XLU", "WMT", "COST", "VAS"),
    "inflation": ("XLE", "XOM", "GLD", "CAT", "BA", "BHP", "RIO", "FMG"),
}

REGIME_THEMES: dict[str, tuple[str, ...]] = {
    "risk_on": ("risk_on", "tech", "growth", "asx_etf"),
    "risk_off": ("risk_off", "finance", "energy", "asx_banks"),
    "transition": ("tech", "finance", "consumer", "resources"),
    "crisis": ("risk_off", "energy", "asx_banks"),
}


def combined_seed_pool(settings: Settings) -> list[str]:
    """US allowlist ∪ AU allowlist (when AU venue enabled)."""
    from app.market.venues import Venue, enabled_venues

    out: list[str] = []
    seen: set[str] = set()
    for raw in settings.trade_allowlist:
        sym = raw.upper().strip()
        if sym and sym not in seen:
            out.append(sym)
            seen.add(sym)
    if Venue.AU in enabled_venues(settings):
        for raw in settings.trade_allowlist_au:
            sym = raw.upper().strip()
            if sym and sym not in seen:
                out.append(sym)
                seen.add(sym)
    return out


def venue_for_universe_symbol(settings: Settings, symbol: str) -> str:
    """Best-effort venue tag for watchlist payload (AU allowlist/candidates → AU)."""
    from app.market.venues import Venue, enabled_venues, venue_for_symbol

    sym = symbol.upper().strip()
    if Venue.AU in enabled_venues(settings):
        au = {s.upper() for s in settings.trade_allowlist_au}
        au |= set(DEFAULT_CANDIDATE_POOL_AU)
        if sym in au:
            return Venue.AU.value
    return venue_for_symbol(sym, settings).value


def curated_candidate_pool(settings: Settings | None = None) -> list[str]:
    """Configured pool if set; otherwise built-in US (+ AU when enabled) list."""
    if settings is not None and settings.universe_candidate_pool:
        return [s.upper().strip() for s in settings.universe_candidate_pool if s.strip()]
    from app.market.venues import Venue, enabled_venues

    out = list(DEFAULT_CANDIDATE_POOL_US)
    if settings is not None and Venue.AU in enabled_venues(settings):
        seen = set(out)
        for sym in DEFAULT_CANDIDATE_POOL_AU:
            if sym not in seen:
                out.append(sym)
                seen.add(sym)
    return out


def themes_for_regime(market_regime: str | None) -> list[str]:
    if not market_regime:
        return []
    key = market_regime.strip().lower().replace("-", "_").replace(" ", "_")
    # Map firm MarketRegime enum values onto theme buckets.
    aliases = {
        "strong_risk_on": "risk_on",
        "risk_on": "risk_on",
        "neutral": "transition",
        "risk_off": "risk_off",
        "strong_risk_off": "risk_off",
        "crisis": "crisis",
        "transition": "transition",
        "insufficient_data": "",
    }
    mapped = aliases.get(key, key)
    if not mapped:
        return []
    return list(REGIME_THEMES.get(mapped, ()))


def ranked_candidate_pool(
    settings: Settings | None = None,
    *,
    themes: list[str] | None = None,
    market_regime: str | None = None,
) -> list[str]:
    """Return candidate symbols with theme/regime matches first (stable within tiers)."""
    base = curated_candidate_pool(settings)
    tags = [t.strip().lower() for t in (themes or []) if t and str(t).strip()]
    tags.extend(themes_for_regime(market_regime))
    if not tags:
        return base

    boosted: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        for sym in THEME_SYMBOLS.get(tag, ()):
            s = sym.upper()
            if s in seen or s not in base:
                continue
            boosted.append(s)
            seen.add(s)
    for s in base:
        if s not in seen:
            boosted.append(s)
            seen.add(s)
    return boosted


def addable_universe(
    settings: Settings,
    *,
    known_symbols: set[str] | None = None,
    candidate_symbols: set[str] | list[str] | None = None,
) -> set[str]:
    """Symbols the Universe Manager may newly add (seed ∪ candidates ∪ known)."""
    seed = set(combined_seed_pool(settings))
    known = {s.upper() for s in (known_symbols or set())}
    if not settings.universe_allow_candidate_adds:
        return seed | known
    if candidate_symbols is not None:
        cand = {s.upper() for s in candidate_symbols}
    else:
        cand = set(curated_candidate_pool(settings))
    return seed | known | cand
