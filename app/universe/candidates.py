"""Curated expansion pool beyond TRADE_ALLOWLIST for Universe Manager adds.

Not a full-market screener — a bounded, liquid candidate set the AI may promote
onto the watchlist. Seed (TRADE_ALLOWLIST) remains the bootstrap soft boundary;
candidates expand it without inventing obscure tickers.

Optional theme / regime ranking reorders the pool so focus-adjacent names float up.
"""

from __future__ import annotations

from app.core.config import Settings

# Liquid US names / sector ETFs commonly useful across horizons.
DEFAULT_CANDIDATE_POOL: tuple[str, ...] = (
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

# Theme tag → symbols to boost (must still be in candidate / seed to be addable).
THEME_SYMBOLS: dict[str, tuple[str, ...]] = {
    "tech": ("XLK", "SMH", "SOXX", "NVDA", "AMD", "AVGO", "AAPL", "MSFT", "GOOGL", "META", "CRM", "ORCL", "NOW", "SNOW"),
    "semiconductor": ("SMH", "SOXX", "NVDA", "AMD", "AVGO", "MU", "INTC"),
    "ai": ("NVDA", "MSFT", "GOOGL", "META", "PLTR", "AMD", "AVGO", "SMH", "CRWD"),
    "finance": ("XLF", "JPM", "GS", "V", "MA", "PYPL", "COIN"),
    "energy": ("XLE", "XOM"),
    "biotech": ("XBI", "UNH"),
    "consumer": ("COST", "WMT", "HD", "DIS", "NFLX", "UBER", "SHOP"),
    "cyber": ("CRWD", "PANW"),
    "growth": ("ARKK", "TSLA", "SHOP", "COIN", "PLTR", "NFLX"),
    "risk_on": ("QQQ", "XLK", "SMH", "IWM", "ARKK", "NVDA", "TSLA"),
    "risk_off": ("SPY", "DIA", "XLU", "TLT", "GLD", "JPM", "XOM", "WMT"),
    "deflation": ("TLT", "GLD", "XLU", "WMT", "COST"),
    "inflation": ("XLE", "XOM", "GLD", "CAT", "BA"),
}

REGIME_THEMES: dict[str, tuple[str, ...]] = {
    "risk_on": ("risk_on", "tech", "growth"),
    "risk_off": ("risk_off", "finance", "energy"),
    "transition": ("tech", "finance", "consumer"),
    "crisis": ("risk_off", "energy"),
}


def curated_candidate_pool(settings: Settings | None = None) -> list[str]:
    """Configured pool if set; otherwise built-in curated list."""
    if settings is not None and settings.universe_candidate_pool:
        return [s.upper().strip() for s in settings.universe_candidate_pool if s.strip()]
    return list(DEFAULT_CANDIDATE_POOL)


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
    seed = {s.upper() for s in settings.trade_allowlist}
    known = {s.upper() for s in (known_symbols or set())}
    if not settings.universe_allow_candidate_adds:
        return seed | known
    if candidate_symbols is not None:
        cand = {s.upper() for s in candidate_symbols}
    else:
        cand = set(curated_candidate_pool(settings))
    return seed | known | cand
