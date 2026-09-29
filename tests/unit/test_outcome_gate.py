"""Closed-trade memory for new entries."""

from app.universe.outcome_gate import entry_block_reason


def test_five_losses_and_no_win_blocks() -> None:
    assert entry_block_reason([-10, -10, -10, -10]) is None
    assert entry_block_reason([-10, -10, -10, -10, -10]) == "no_win_after_closes"


def test_a_win_keeps_the_name_until_the_recent_window_is_red() -> None:
    mixed = [100.0, -10, -10, -10, -10]
    assert entry_block_reason(mixed) is None
    recent = [50.0] + [-10.0] * 9
    assert entry_block_reason(recent) == "recent_net_loss"
    assert entry_block_reason([20.0] * 10) is None


def test_empty_ledger_does_not_block() -> None:
    assert entry_block_reason(None) is None
    assert entry_block_reason([]) is None
