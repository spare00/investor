"""Horizon-aware intraday re-evaluation cadence helpers."""

from __future__ import annotations

import math

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
    return max(
        1.0,
        reeval_seconds_for_horizon(horizon_by_symbol.get(symbol.upper()), settings) / 60.0,
    )


def global_reeval_gap_minutes(
    symbols: list[str],
    horizon_by_symbol: dict[str, str],
    settings: Settings,
) -> float:
    """Global cooldown = tightest book among symbols under review."""
    return max(1.0, min_reeval_seconds_for_symbols(symbols, horizon_by_symbol, settings) / 60.0)


def planned_intraday_interval_minutes(
    horizons: list[str] | None,
    settings: Settings,
    *,
    session_minutes: float | None = None,
) -> int:
    """Minutes between planned ``intraday_eval_*`` scheduler jobs.

    Uses the tightest horizon among the books under review (open positions or
    focus/entry set). Falls back to ``intraday_reevaluation_interval_minutes``
    when empty. Callers should pass open/focus horizons — not the full watchlist —
    so a single scalp seed does not densify a medium-only session plan.

    When ``session_minutes`` is set, cloud (billable) spacing is also floored so
    we do not plan more than roughly ``1.5 * max_intraday_reanalyses`` ticks.
    Local/embedded runtimes skip that spend floor — cadence follows the book.
    """
    fallback = max(1, int(settings.intraday_reevaluation_interval_minutes))
    cleaned = [h for h in (horizons or []) if h]
    if cleaned:
        secs = min(reeval_seconds_for_horizon(h, settings) for h in cleaned)
        horizon_mins = max(1, int(math.ceil(secs / 60.0)))
    else:
        horizon_mins = fallback

    if session_minutes is not None and session_minutes > 0 and not settings.llm_is_local():
        max_jobs = max(4, int(math.ceil(int(effective_max_intraday_reanalyses(settings)) * 1.5)))
        budget_mins = max(1, int(math.ceil(float(session_minutes) / max_jobs)))
        return max(horizon_mins, budget_mins)
    return horizon_mins


def effective_max_intraday_reanalyses(settings: Settings) -> int:
    """Cloud spend cap vs local loop cap."""
    if settings.llm_is_local():
        return max(1, int(settings.max_intraday_reanalyses_local))
    return max(1, int(settings.max_intraday_reanalyses))
