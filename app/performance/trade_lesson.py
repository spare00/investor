"""One close, two judgments: execution and strategy.

A broken order is not a strategy result. A single faithful close is recorded
and does not change the playbook. A draft of the next strategy version is
written only after a pre-set count of faithful closes, and it is not applied.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from app.brokers.models import canonical_order_type
from app.brokers.venue_orders import is_resting_stop
from app.universe.outcome_gate import entry_block_reason

STRATEGY_VERSION = "book-v1"
MIN_FAITHFUL_CLOSES = 30


def strategy_id(horizon: str | None, entry_reason: str | None = None) -> str:
    book = str(horizon or "unknown").strip().lower() or "unknown"
    reason = str(entry_reason or "untagged").strip().lower() or "untagged"
    return f"{book}:{reason}@{STRATEGY_VERSION}"


@dataclass(frozen=True, slots=True)
class CloseFacts:
    symbol: str
    horizon: str | None = None
    entry_reason: str | None = None
    intended_order_type: str | None = None
    broker_order_type: str | None = None
    gross_pnl: float | None = None


@dataclass(frozen=True, slots=True)
class TradeLesson:
    symbol: str
    strategy_id: str
    layer: str
    execution_verdict: str
    strategy_verdict: str | None
    cause: str
    action: str
    counts_for_strategy: bool
    gross_pnl: float | None
    applied: bool = False


def select_exit_order(
    orders: list[dict[str, str | None]],
) -> tuple[str | None, str | None]:
    """Pick the exit row to judge. A filled protective stop wins over other sells."""

    def rank(order: dict[str, str | None]) -> tuple[bool, bool, bool]:
        key = str(order.get("idempotency_key") or "")
        status = str(order.get("status") or "").lower()
        filled = status in {"filled", "partially_filled"}
        protect = key.startswith("protect-stop:")
        return (protect and filled, protect, filled)

    if not orders or not any(any(rank(order)) for order in orders):
        return None, None
    chosen = max(orders, key=rank)
    return (
        str(chosen.get("idempotency_key") or "") or None,
        str(chosen.get("order_type") or "") or None,
    )


def intended_and_broker(idempotency_key: str | None, order_type: str | None) -> tuple[str, str]:
    """What we meant, and what the order row says was sent."""
    key = str(idempotency_key or "")
    broker = canonical_order_type(order_type)
    if key.startswith("protect-stop:"):
        return "stop", broker
    if key.startswith("force-close:") or key.startswith("hard-stop:"):
        return "market", broker
    return broker, broker


def execution_verdict(intended: str | None, broker: str | None) -> tuple[str, str]:
    """Return (verdict, cause).

    An AU market order that becomes a limit is faithful. A protective stop
    that becomes a limit is not.
    """
    if not str(intended or "").strip() or not str(broker or "").strip():
        return "unknown", "execution_unobserved"
    want = canonical_order_type(intended)
    sent = canonical_order_type(broker)
    if is_resting_stop(want) and not is_resting_stop(sent):
        return "broken", "protective_stop_sent_as_limit"
    if want == "market" and sent == "limit":
        return "faithful", "marketable_limit"
    if want != sent:
        return "broken", "broker_order_type_mismatch"
    return "faithful", "order_type_matched"


def judge_close(facts: CloseFacts) -> TradeLesson:
    verdict, cause = execution_verdict(facts.intended_order_type, facts.broker_order_type)
    sid = strategy_id(facts.horizon, facts.entry_reason)
    symbol = str(facts.symbol or "").upper()
    if verdict == "broken":
        return TradeLesson(
            symbol=symbol,
            strategy_id=sid,
            layer="execution",
            execution_verdict="broken",
            strategy_verdict=None,
            cause=cause,
            action="exclude_from_strategy",
            counts_for_strategy=False,
            gross_pnl=facts.gross_pnl,
        )
    if verdict != "faithful" or facts.gross_pnl is None:
        withheld = "pnl_unknown" if verdict == "faithful" else cause
        return TradeLesson(
            symbol=symbol,
            strategy_id=sid,
            layer="withheld",
            execution_verdict=verdict,
            strategy_verdict=None,
            cause=withheld,
            action="record_only",
            counts_for_strategy=False,
            gross_pnl=facts.gross_pnl,
        )
    pnl = float(facts.gross_pnl)
    if pnl > 0:
        outcome = "win"
    elif pnl < 0:
        outcome = "loss"
    else:
        outcome = "scratch"
    recorded = cause if cause not in {"order_type_matched"} else "faithful_close"
    return TradeLesson(
        symbol=symbol,
        strategy_id=sid,
        layer="strategy",
        execution_verdict="faithful",
        strategy_verdict=outcome,
        cause=recorded,
        action="record_only",
        counts_for_strategy=True,
        gross_pnl=pnl,
    )


def lesson_to_dict(lesson: TradeLesson) -> dict[str, Any]:
    return asdict(lesson)


def lesson_from_dict(raw: dict[str, Any]) -> TradeLesson | None:
    if not raw or not raw.get("strategy_id"):
        return None
    try:
        pnl = None if raw.get("gross_pnl") is None else float(raw["gross_pnl"])
    except (TypeError, ValueError):
        pnl = None
    return TradeLesson(
        symbol=str(raw.get("symbol") or "").upper(),
        strategy_id=str(raw["strategy_id"]),
        layer=str(raw.get("layer") or "withheld"),
        execution_verdict=str(raw.get("execution_verdict") or "unknown"),
        strategy_verdict=raw.get("strategy_verdict"),
        cause=str(raw.get("cause") or ""),
        action=str(raw.get("action") or "record_only"),
        counts_for_strategy=bool(raw.get("counts_for_strategy")),
        gross_pnl=pnl,
        applied=bool(raw.get("applied")),
    )


def feedback_report(
    lessons: list[TradeLesson],
    *,
    min_faithful_closes: int = MIN_FAITHFUL_CLOSES,
) -> dict[str, Any]:
    """Oldest-first lessons. Nothing in the report is marked applied."""
    by_strategy: dict[str, list[TradeLesson]] = {}
    breaks: dict[str, list[str]] = {}
    faithful_by_symbol: dict[str, list[float]] = {}
    for lesson in lessons:
        if lesson.execution_verdict == "broken":
            breaks.setdefault(lesson.cause, [])
            if lesson.symbol and lesson.symbol not in breaks[lesson.cause]:
                breaks[lesson.cause].append(lesson.symbol)
        if not lesson.counts_for_strategy or lesson.gross_pnl is None:
            continue
        by_strategy.setdefault(lesson.strategy_id, []).append(lesson)
        faithful_by_symbol.setdefault(lesson.symbol, []).append(float(lesson.gross_pnl))

    strategies: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []
    floor = max(1, int(min_faithful_closes))
    for sid, rows in sorted(by_strategy.items()):
        pnls = [float(row.gross_pnl or 0.0) for row in rows]
        wins = sum(1 for value in pnls if value > 0)
        losses = sum(1 for value in pnls if value < 0)
        scratches = sum(1 for value in pnls if value == 0)
        gross = round(sum(pnls), 4)
        expectancy = round(gross / len(pnls), 4) if pnls else None
        strategies.append(
            {
                "strategy_id": sid,
                "closes": len(pnls),
                "wins": wins,
                "losses": losses,
                "scratches": scratches,
                "gross_pnl": gross,
                "expectancy": expectancy,
            }
        )
        if len(pnls) < floor:
            proposals.append(
                {
                    "action": "keep_running",
                    "strategy_id": sid,
                    "reason": f"{len(pnls)} faithful closes, {floor} required before a draft",
                    "applied": False,
                }
            )
            continue
        if expectancy is not None and expectancy < 0:
            proposals.append(
                {
                    "action": "draft_next_strategy",
                    "strategy_id": sid,
                    "reason": f"faithful expectancy {expectancy} over {len(pnls)} closes",
                    "applied": False,
                }
            )
        else:
            proposals.append(
                {
                    "action": "keep_strategy",
                    "strategy_id": sid,
                    "reason": f"faithful expectancy {expectancy} over {len(pnls)} closes",
                    "applied": False,
                }
            )

    for cause, symbols in sorted(breaks.items()):
        proposals.append(
            {
                "action": "fix_execution",
                "cause": cause,
                "symbols": symbols,
                "closes": sum(1 for lesson in lessons if lesson.cause == cause),
                "reason": "broken execution is excluded from strategy results",
                "applied": False,
            }
        )

    for symbol, pnls in sorted(faithful_by_symbol.items()):
        reason = entry_block_reason(pnls)
        if reason is None:
            continue
        proposals.append(
            {
                "action": "consider_symbol_block",
                "symbol": symbol,
                "reason": reason,
                "applied": False,
            }
        )

    return {
        "strategy_version": STRATEGY_VERSION,
        "min_faithful_closes": floor,
        "lesson_count": len(lessons),
        "strategy_close_count": sum(row["closes"] for row in strategies),
        "by_strategy": strategies,
        "execution_breaks": [
            {"cause": cause, "symbols": symbols, "names": len(symbols)}
            for cause, symbols in sorted(breaks.items())
        ],
        "proposals": proposals,
        "auto_applied": False,
    }
