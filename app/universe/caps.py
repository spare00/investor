"""Per-horizon open-position caps for AI-managed books."""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from app.universe.horizons import UniverseHorizon, policy_for


def count_open_by_horizon(
    held_symbols: Iterable[str],
    horizon_by_symbol: dict[str, str],
) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for raw in held_symbols:
        sym = str(raw).upper()
        if not sym:
            continue
        h = (horizon_by_symbol.get(sym) or UniverseHorizon.SHORT.value).lower()
        counts[h] += 1
    return dict(counts)


def horizon_cap_violation(
    *,
    symbol: str,
    horizon_by_symbol: dict[str, str],
    held_symbols: Iterable[str],
    is_new_symbol: bool,
) -> str | None:
    """Return rejection reason if adding this symbol would exceed its book cap."""
    if not is_new_symbol:
        return None
    sym = symbol.upper()
    horizon = (horizon_by_symbol.get(sym) or UniverseHorizon.SHORT.value).lower()
    try:
        policy = policy_for(horizon)
    except ValueError:
        return None
    held = {str(s).upper() for s in held_symbols if s}
    # Already held → not a new slot
    if sym in held:
        return None
    counts = count_open_by_horizon(held, horizon_by_symbol)
    current = counts.get(horizon, 0)
    if current >= policy.max_positions:
        return (
            f"{sym}:horizon_cap:{horizon}:{current}>="
            f"{policy.max_positions}({policy.label_ko})"
        )
    return None
