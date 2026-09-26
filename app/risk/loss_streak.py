"""Current losing streak and the cooldown it implies.

Kept pure (no session, no settings lookup) so the veto arithmetic stays
testable without a database, matching the rest of ``app/risk``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True)
class LossStreak:
    consecutive_losses: int
    cooldown_until: datetime | None
    last_loss_at: datetime | None


def loss_streak(
    closes: Sequence[tuple[datetime | None, float | None]],
    *,
    cooldown_minutes: int,
) -> LossStreak:
    """Count back from the newest close until a win breaks the run.

    ``closes`` must be newest-first as ``(closed_at, realized_pl)``. Trades with
    no measurable P&L (``None``) are skipped rather than counted or treated as a
    win — an unknown outcome must not quietly reset a streak and reopen the gate.
    A flat close is likewise neutral.
    """
    streak = 0
    last_loss_at: datetime | None = None
    for closed_at, pnl in closes:
        if pnl is None:
            continue
        value = float(pnl)
        if value > 0:
            break
        if value == 0:
            continue
        streak += 1
        if last_loss_at is None:
            last_loss_at = closed_at

    cooldown_until: datetime | None = None
    if streak and last_loss_at is not None and cooldown_minutes > 0:
        cooldown_until = last_loss_at + timedelta(minutes=cooldown_minutes)
    return LossStreak(
        consecutive_losses=streak,
        cooldown_until=cooldown_until,
        last_loss_at=last_loss_at,
    )
