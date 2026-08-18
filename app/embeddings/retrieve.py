"""Horizon- and agent-aware retrieval policies."""

from __future__ import annotations

# What each agent should see. Full JSON dumps mix all of this into every call.
AGENT_SOURCE_TYPES: dict[str, tuple[str, ...]] = {
    "market_intelligence": ("news",),
    "macro_strategist": ("macro", "news"),
    "quant_strategist": ("market",),
    "risk_manager": ("market", "macro"),
    "devils_advocate": ("news", "market", "macro"),
    "cio": ("news", "market", "macro", "watchlist"),
    "universe_manager": ("watchlist", "market", "news"),
}

# Style books overweight different evidence. Shared pipeline ignored this.
HORIZON_SOURCE_TYPES: dict[str, tuple[str, ...]] = {
    "scalp": ("news", "market"),
    "day": ("news", "market"),
    "short": ("news", "market", "macro"),
    "medium": ("macro", "market", "watchlist"),
}


def source_types_for(*, agent: str | None, horizon: str | None) -> list[str]:
    agent_key = (agent or "").strip().lower()
    hz = (horizon or "").strip().lower()
    agent_types = AGENT_SOURCE_TYPES.get(agent_key)
    hz_types = HORIZON_SOURCE_TYPES.get(hz)
    if agent_types and hz_types:
        return [t for t in agent_types if t in hz_types] or list(agent_types)
    if agent_types:
        return list(agent_types)
    if hz_types:
        return list(hz_types)
    return ["news", "market", "macro"]


def query_text(*, agent: str | None, symbols: list[str], extra: str = "") -> str:
    names = " ".join(s.upper() for s in symbols if s)
    hint = {
        "market_intelligence": "news catalysts filings events",
        "quant_strategist": "price trend rsi atr gap spread",
        "macro_strategist": "rates inflation regime credit oil",
        "risk_manager": "exposure stop liquidity drawdown",
        "devils_advocate": "priced in crowding missing data wait",
        "cio": "entry thesis invalidation portfolio action",
        "universe_manager": "watchlist horizon liquidity theme",
    }.get((agent or "").strip().lower(), "market context")
    return f"{hint} {names} {extra}".strip()
