"""Standing sleeve weights: cash 20 and four books of 20."""

from app.universe.allocation import (
    CASH_HARD_PCT,
    CASH_SOFT_PCT,
    CASH_TARGET_PCT,
    SLEEVE_TARGET_PCT,
    assigned_horizon,
    deployable_cash_pct,
    entry_notional_pct,
    sleeve_ceiling,
    sleeve_room,
    sleeve_target,
)


def test_sleeves_and_cash_use_the_whole_book() -> None:
    assert CASH_SOFT_PCT == 20.0
    assert CASH_TARGET_PCT == CASH_SOFT_PCT
    assert CASH_HARD_PCT == 10.0
    assert sum(SLEEVE_TARGET_PCT.values()) == 80.0
    assert set(SLEEVE_TARGET_PCT) == {"scalp", "day", "short", "medium"}


def test_settings_keep_twenty_percent_cash_soft_and_ten_percent_hard() -> None:
    from app.core.config import Settings
    from app.risk.types import RiskLimits

    assert Settings.model_fields["cash_soft_pct"].default == 20.0
    assert Settings.model_fields["min_cash_pct"].default == 10.0
    assert Settings.model_fields["max_gross_exposure_pct"].default == 90.0
    limits = RiskLimits()
    assert limits.min_cash_pct == 10.0
    assert limits.max_gross_exposure_pct == 90.0


def test_cash_between_the_soft_target_and_the_hard_floor_can_fund_a_sleeve() -> None:
    assert deployable_cash_pct(cash_pct=16.0, hard_floor_pct=CASH_HARD_PCT) == 6.0
    assert deployable_cash_pct(cash_pct=10.0, hard_floor_pct=CASH_HARD_PCT) == 0.0
    assert deployable_cash_pct(cash_pct=100.0, hard_floor_pct=CASH_HARD_PCT) == 90.0


def test_a_sleeve_may_sit_a_little_over_or_under_its_aim() -> None:
    assert sleeve_target("medium") == 20.0
    assert sleeve_ceiling("medium") == 25.0
    assert sleeve_room("medium", 12.0) == 13.0
    assert sleeve_room("medium", 20.0) == 5.0
    assert sleeve_room("medium", 25.0) == 0.0
    assert (
        entry_notional_pct(
            horizon="medium",
            used_pct=20.0,
            max_position_pct=10.0,
            target_size_pct=10.0,
        )
        == 5.0
    )
    assert (
        entry_notional_pct(
            horizon="scalp",
            used_pct=25.0,
            max_position_pct=10.0,
            target_size_pct=10.0,
        )
        == 0.0
    )


def test_indexes_are_the_medium_sleeve_and_tape_names_stay_on_their_clock() -> None:
    for sym in ("SPY", "QQQ", "DIA", "VAS", "IOZ", "NDQ"):
        assert assigned_horizon(sym) == "medium"
    assert assigned_horizon("NVDA") == "scalp"
    assert assigned_horizon("TSLA") == "scalp"
    assert assigned_horizon("IWM") == "day"
    assert assigned_horizon("BHP") is None
