"""Paper-trading relaxations for data fail-closed gates.

Live trading keeps strict fail-closed behaviour. Paper runs may proceed when
core US index overlays are incomplete but venue symbols still have IBKR quotes.
"""

from __future__ import annotations

from app.core.config import Settings, TradingMode, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_SOFT_INDEX_REASON = "missing_core_index_data"


def paper_relaxed_data_gates(settings: Settings | None = None) -> bool:
    cfg = settings or get_settings()
    return bool(
        cfg.paper_relaxed_data_gates
        and cfg.trading_mode == TradingMode.PAPER
        and not cfg.live_trading_enabled
    )


def relax_fail_closed_reasons(
    reasons: list[str],
    *,
    quote_count: int,
    settings: Settings | None = None,
) -> tuple[list[str], list[str]]:
    """Drop soft collection reasons on paper when usable quotes exist."""
    if not paper_relaxed_data_gates(settings) or not reasons or quote_count <= 0:
        return list(reasons), []

    kept = list(reasons)
    warnings: list[str] = []
    soft_only = all(
        r == _SOFT_INDEX_REASON or r.startswith("quality_hard_fail:")
        for r in kept
    )
    if not soft_only:
        return kept, warnings

    if _SOFT_INDEX_REASON in kept:
        kept = [r for r in kept if r != _SOFT_INDEX_REASON]
        warnings.append(f"paper_relaxed:{_SOFT_INDEX_REASON}")

    if kept != reasons:
        logger.warning(
            "paper_fail_closed_relaxed",
            quote_count=quote_count,
            dropped=[r for r in reasons if r not in kept],
        )
    return kept, warnings
