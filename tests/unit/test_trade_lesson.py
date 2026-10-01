"""Execution failures stay out of the strategy score, and drafts are not applied."""

from __future__ import annotations

from app.performance.trade_lesson import (
    MIN_FAITHFUL_CLOSES,
    CloseFacts,
    feedback_report,
    judge_close,
    select_exit_order,
)


def test_protective_stop_sent_as_limit_is_not_a_strategy_loss() -> None:
    lesson = judge_close(
        CloseFacts(
            symbol="cba",
            horizon="short",
            entry_reason="oversold_bounce",
            intended_order_type="stop",
            broker_order_type="limit",
            gross_pnl=-409.57,
        )
    )
    assert lesson.execution_verdict == "broken"
    assert lesson.cause == "protective_stop_sent_as_limit"
    assert lesson.counts_for_strategy is False
    assert lesson.strategy_verdict is None
    assert lesson.action == "exclude_from_strategy"
    assert lesson.applied is False
    assert lesson.strategy_id == "short:oversold_bounce@book-v1"


def test_faithful_stop_loss_counts_and_does_not_change_the_book() -> None:
    lesson = judge_close(
        CloseFacts(
            symbol="BHP",
            horizon="short",
            intended_order_type="stop",
            broker_order_type="stop",
            gross_pnl=-12.5,
        )
    )
    assert lesson.execution_verdict == "faithful"
    assert lesson.strategy_verdict == "loss"
    assert lesson.counts_for_strategy is True
    assert lesson.action == "record_only"
    assert lesson.applied is False


def test_au_flatten_limit_is_a_faithful_exit() -> None:
    lesson = judge_close(
        CloseFacts(
            symbol="CBA",
            horizon="day",
            intended_order_type="market",
            broker_order_type="limit",
            gross_pnl=4.0,
        )
    )
    assert lesson.execution_verdict == "faithful"
    assert lesson.cause == "marketable_limit"
    assert lesson.strategy_verdict == "win"


def test_missing_pnl_is_withheld() -> None:
    lesson = judge_close(
        CloseFacts(
            symbol="CBA",
            horizon="short",
            intended_order_type="stop",
            broker_order_type="stop",
            gross_pnl=None,
        )
    )
    assert lesson.counts_for_strategy is False
    assert lesson.cause == "pnl_unknown"
    assert lesson.strategy_verdict is None


def test_filled_protect_stop_is_the_exit_we_judge() -> None:
    key, order_type = select_exit_order(
        [
            {"idempotency_key": "buy:1", "order_type": "limit", "status": "filled"},
            {
                "idempotency_key": "protect-stop:abc",
                "order_type": "limit",
                "status": "FILLED",
            },
        ]
    )
    assert key == "protect-stop:abc"
    assert order_type == "limit"


def test_report_waits_for_the_sample_and_does_not_apply() -> None:
    faithful = [
        judge_close(
            CloseFacts(
                symbol="SPY",
                horizon="scalp",
                entry_reason="uptrend_dip",
                intended_order_type="stop",
                broker_order_type="stop",
                gross_pnl=-1.0,
            )
        )
        for _ in range(3)
    ]
    broken = judge_close(
        CloseFacts(
            symbol="CBA",
            horizon="short",
            intended_order_type="stop",
            broker_order_type="limit",
            gross_pnl=-50.0,
        )
    )
    report = feedback_report([*faithful, broken])
    assert report["auto_applied"] is False
    assert report["strategy_close_count"] == 3
    assert report["by_strategy"][0]["gross_pnl"] == -3.0
    assert report["by_strategy"][0]["closes"] < MIN_FAITHFUL_CLOSES
    assert any(row["action"] == "keep_running" for row in report["proposals"])
    assert not any(row["action"] == "draft_next_strategy" for row in report["proposals"])
    assert any(row["action"] == "fix_execution" for row in report["proposals"])
    assert all(row["applied"] is False for row in report["proposals"])


def test_draft_appears_only_after_enough_faithful_losses() -> None:
    lessons = [
        judge_close(
            CloseFacts(
                symbol="SPY",
                horizon="scalp",
                entry_reason="chase",
                intended_order_type="stop",
                broker_order_type="stp",
                gross_pnl=-1.0,
            )
        )
        for _ in range(MIN_FAITHFUL_CLOSES)
    ]
    report = feedback_report(lessons)
    drafts = [row for row in report["proposals"] if row["action"] == "draft_next_strategy"]
    assert len(drafts) == 1
    assert drafts[0]["applied"] is False
    assert drafts[0]["strategy_id"] == "scalp:chase@book-v1"


def test_entry_trial_is_not_rewritten_after_it_is_frozen() -> None:
    from app.performance.trade_lesson import freeze_entry_trial

    first = freeze_entry_trial(
        None,
        {
            "horizon": "scalp",
            "entry_reason": "chase",
            "score": 0.62,
            "stop_price": 99.0,
            "currency": "AUD",
        },
    )
    later = freeze_entry_trial(
        first,
        {"horizon": "short", "entry_reason": "oversold_bounce", "score": 0.9},
    )
    assert later["strategy_id"] == "scalp:chase@book-v1"
    assert later["horizon"] == "scalp"
    assert later["score_kind"] == "heuristic"
    assert later is not first

    lesson = judge_close(
        CloseFacts(
            symbol="CBA",
            horizon="short",
            entry_reason="oversold_bounce",
            strategy_id=later["strategy_id"],
            intended_order_type="stop",
            broker_order_type="stop",
            gross_pnl=-1.0,
        )
    )
    assert lesson.strategy_id == "scalp:chase@book-v1"
