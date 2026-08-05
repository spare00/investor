"""Horizon-aware intraday re-evaluation cadence helpers."""

from __future__ import annotations

from app.core.config import Settings
from app.universe.horizons import policy_for


def reeval_seconds_for_horizon(horizon: str | None, settings: Settings) -> int:
    """Seconds between re-evals for a book; falls back to global min."""
    fallback = max(60, int(settings.intraday_min_reeval_seconds))
    if not horizon:
        return fallback
    try:
        return max(60, int(policy_for(horizon).reeval_seconds))
    except ValueError:
        return fallback


def min_reeval_seconds_for_symbols(
    symbols: list[str],
    horizon_by_symbol: dict[str, str],
    settings: Settings,
) -> int:
    """Tightest (shortest) re-eval among open symbols; global default if none."""
    if not symbols:
        return max(60, int(settings.intraday_min_reeval_seconds))
    seconds = [
        reeval_seconds_for_horizon(horizon_by_symbol.get(s.upper()), settings) for s in symbols
    ]
    return max(60, min(seconds))


def symbol_reeval_gap_minutes(
    symbol: str,
    horizon_by_symbol: dict[str, str],
    settings: Settings,
) -> float:
    """Per-symbol cooldown in minutes from watchlist horizon policy."""
    return max(1.0, reeval_seconds_for_horizon(horizon_by_symbol.get(symbol.upper()), settings) / 60.0)


def global_reeval_gap_minutes(
    symbols: list[str],
    horizon_by_symbol: dict[str, str],
    settings: Settings,
) -> float:
    """Global cooldown = tightest book among symbols under review."""
    return max(1.0, min_reeval_seconds_for_symbols(symbols, horizon_by_symbol, settings) / 60.0)
