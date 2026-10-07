"""Standing capital sleeves. Each book trades inside its own budget.

Cash prefers 20%. That 20% is a soft buffer the horizon books may spend
when a sleeve still has room. Risk stops a buy that would leave cash
under 10%. A sideways scalp book stays underweight until its own setup
appears, and that spare sits in cash above the soft target.
"""

from __future__ import annotations

# Preferred cash. Horizon books may spend it down toward the hard floor.
CASH_SOFT_PCT = 20.0
CASH_TARGET_PCT = CASH_SOFT_PCT
# Risk veto. Cash is not allowed through this.
CASH_HARD_PCT = 10.0

# Share of equity each clock aims to hold.
SLEEVE_TARGET_PCT: dict[str, float] = {
    "scalp": 20.0,
    "day": 20.0,
    "short": 20.0,
    "medium": 20.0,
}
# A book may sit this far over its aim. Cash still cannot fall through 10%.
SLEEVE_BAND_PCT = 5.0

# Broad indexes that compound. They live in the medium sleeve.
STABLE_HOLD_SYMBOLS = frozenset({"SPY", "QQQ", "DIA", "VAS", "IOZ", "NDQ"})

# Same-day tape. Not the index sleeve.
SCALP_SYMBOLS = frozenset({"NVDA", "TSLA"})

# Same session, flatten before the close.
DAY_SYMBOLS = frozenset({"IWM", "META", "AMZN", "GOOGL"})


def deployable_cash_pct(*, cash_pct: float, hard_floor_pct: float) -> float:
    """Cash a buy may use. The soft 20% is not reserved."""
    return max(0.0, round(float(cash_pct) - float(hard_floor_pct), 2))


def sleeve_target(horizon: str | None) -> float:
    return float(SLEEVE_TARGET_PCT.get(str(horizon or "").strip().lower(), 0.0))


def sleeve_ceiling(horizon: str | None) -> float:
    """Aim plus a small band. Zero when the horizon is not a standing sleeve."""
    target = sleeve_target(horizon)
    if target <= 0:
        return 0.0
    return round(target + SLEEVE_BAND_PCT, 2)


def sleeve_room(horizon: str | None, used_pct: float) -> float:
    return max(0.0, round(sleeve_ceiling(horizon) - float(used_pct or 0.0), 2))


def assigned_horizon(symbol: str | None) -> str | None:
    """Standing book for a symbol. None means the universe may choose."""
    sym = str(symbol or "").upper().strip()
    if sym in STABLE_HOLD_SYMBOLS:
        return "medium"
    if sym in SCALP_SYMBOLS:
        return "scalp"
    if sym in DAY_SYMBOLS:
        return "day"
    return None


def entry_notional_pct(
    *,
    horizon: str | None,
    used_pct: float,
    max_position_pct: float,
    target_size_pct: float | None = None,
) -> float:
    """One new name, capped by the band still left and the position cap."""
    room = sleeve_room(horizon, used_pct)
    cap = float(max_position_pct)
    if target_size_pct is not None:
        cap = min(cap, float(target_size_pct))
    if room <= 0 or cap <= 0:
        return 0.0
    return round(min(cap, room), 2)
