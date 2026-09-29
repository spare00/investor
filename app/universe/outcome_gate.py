"""Block new entries a symbol's own closes have already rejected.

The tape rule will buy the same setup again. These checks are the account's
memory: five closes and no win, or a negative sum over the last ten.
"""

from __future__ import annotations

from collections.abc import Sequence

NO_WIN_CLOSES = 5
RECENT_WINDOW = 10


def entry_block_reason(pnls: Sequence[float] | None) -> str | None:
    """Oldest-first closed P&L. None means the name may still be bought."""
    series = [float(p) for p in (pnls or [])]
    if len(series) >= NO_WIN_CLOSES and not any(p > 0 for p in series):
        return "no_win_after_closes"
    recent = series[-RECENT_WINDOW:]
    if len(recent) >= RECENT_WINDOW and sum(recent) < 0:
        return "recent_net_loss"
    return None
