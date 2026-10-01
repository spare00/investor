"""One trial is the technique at entry plus the result at exit.

Comparable trials were opened under this record, the exit order matched the
intent, and the gross P&L is known. Broken orders and older positions stay
in the table so the path is visible, and they stay out of the method score.
Nothing here changes the playbook.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.intraday.pnl import as_utc
from app.performance.trade_lesson import MIN_FAITHFUL_CLOSES, STRATEGY_VERSION
from app.universe.outcome_gate import entry_block_reason

PATH_TARGET_FIRST = "target_first"
PATH_STOP_FIRST = "stop_first"
PATH_TARGET_ONLY = "target_only"
PATH_STOP_ONLY = "stop_only"
PATH_NEITHER = "neither"
PATH_AMBIGUOUS = "ambiguous"
PATH_UNOBSERVED = "unobserved"

_PATH_ORDER = (
    PATH_STOP_FIRST,
    PATH_TARGET_FIRST,
    PATH_STOP_ONLY,
    PATH_TARGET_ONLY,
    PATH_NEITHER,
    PATH_AMBIGUOUS,
    PATH_UNOBSERVED,
)


def price_path(
    marks: list[tuple[datetime, float]],
    *,
    stop: float | None,
    target: float | None,
) -> str:
    """First long-side print that reaches the stop or the target.

    Marks are snapshot and fill prices. A touch between two marks is not
    visible. One print that reaches both levels is ambiguous.
    """
    if stop is None and target is None:
        return PATH_UNOBSERVED
    ordered = sorted(
        (
            (as_utc(ts), px)
            for ts, px in marks
            if _price(px) is not None and isinstance(ts, datetime)
        ),
        key=lambda item: item[0],
    )
    if not ordered:
        return PATH_UNOBSERVED
    stop_at = None
    target_at = None
    for ts, px in ordered:
        if stop is not None and px <= float(stop) and stop_at is None:
            stop_at = ts
        if target is not None and px >= float(target) and target_at is None:
            target_at = ts
        if stop_at is not None and target_at is not None:
            break
    if stop_at is not None and target_at is not None:
        if target_at < stop_at:
            return PATH_TARGET_FIRST
        if stop_at < target_at:
            return PATH_STOP_FIRST
        return PATH_AMBIGUOUS
    if target_at is not None:
        return PATH_TARGET_ONLY
    if stop_at is not None:
        return PATH_STOP_ONLY
    return PATH_NEITHER


def holding_minutes(opened: datetime | None, closed: datetime | None) -> float | None:
    if opened is None or closed is None:
        return None
    minutes = (closed - opened).total_seconds() / 60.0
    if minutes < 0:
        return None
    return round(minutes, 2)


def counts_for_method(
    *,
    entry_captured_at_open: bool,
    execution_verdict: str | None,
    gross_pnl: float | None,
) -> bool:
    return (
        bool(entry_captured_at_open) and execution_verdict == "faithful" and gross_pnl is not None
    )


def merge_exit(current: dict[str, Any], observed: dict[str, Any]) -> dict[str, Any]:
    """Fill unknown exit facts. A known gross, verdict, or path stays."""
    out = dict(current)
    first = str(current.get("status") or "") != "closed"
    if first or out.get("gross_pnl") is None:
        if observed.get("gross_pnl") is not None:
            out["gross_pnl"] = observed.get("gross_pnl")
    if observed.get("fees_known") and not out.get("fees_known"):
        out["fees_known"] = True
        out["fee"] = observed.get("fee")
    if first or not out.get("closed_at"):
        if observed.get("closed_at") is not None:
            out["closed_at"] = observed.get("closed_at")
    if first or out.get("holding_minutes") is None:
        if observed.get("holding_minutes") is not None:
            out["holding_minutes"] = observed.get("holding_minutes")
    if first or out.get("exit_price") is None:
        if observed.get("exit_price") is not None:
            out["exit_price"] = observed.get("exit_price")
    path = str(out.get("path") or "")
    if first or path in {"", PATH_UNOBSERVED}:
        observed_path = observed.get("path")
        if observed_path:
            out["path"] = observed_path
    verdict = str(out.get("execution_verdict") or "")
    if first or verdict in {"", "unknown"}:
        if observed.get("execution_verdict"):
            out["execution_verdict"] = observed.get("execution_verdict")
            out["cause"] = observed.get("cause")
            out["strategy_verdict"] = observed.get("strategy_verdict")
            out["intended_order_type"] = observed.get("intended_order_type")
            out["broker_order_type"] = observed.get("broker_order_type")
    out["status"] = "closed"
    out["counts_for_method"] = bool(out.get("counts_for_method")) or counts_for_method(
        entry_captured_at_open=bool(out.get("entry_captured_at_open")),
        execution_verdict=out.get("execution_verdict"),
        gross_pnl=out.get("gross_pnl"),
    )
    return out


def trial_feedback(
    rows: list[dict[str, Any]],
    *,
    min_faithful_closes: int = MIN_FAITHFUL_CLOSES,
) -> dict[str, Any]:
    """Score comparable trials by strategy and currency. Apply nothing."""
    floor = max(1, int(min_faithful_closes))
    excluded = {
        "open": 0,
        "entry_not_captured_at_open": 0,
        "execution_broken": 0,
        "pnl_unknown": 0,
    }
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    breaks: dict[str, list[str]] = {}
    faithful_by_symbol: dict[tuple[str, str], list[float]] = {}
    comparable = 0
    for row in rows:
        status = str(row.get("status") or "")
        if status != "closed":
            excluded["open"] += 1
            continue
        if not row.get("entry_captured_at_open"):
            excluded["entry_not_captured_at_open"] += 1
            continue
        verdict = str(row.get("execution_verdict") or "")
        if verdict == "broken":
            excluded["execution_broken"] += 1
            cause = str(row.get("cause") or "execution_broken")
            symbol = str(row.get("symbol") or "").upper()
            breaks.setdefault(cause, [])
            if symbol and symbol not in breaks[cause]:
                breaks[cause].append(symbol)
            continue
        if row.get("gross_pnl") is None or not row.get("counts_for_method"):
            excluded["pnl_unknown"] += 1
            continue
        comparable += 1
        currency = str(row.get("currency") or "UNKNOWN").upper() or "UNKNOWN"
        sid = str(row.get("strategy_id") or "")
        grouped.setdefault((sid, currency), []).append(row)
        faithful_by_symbol.setdefault((str(row.get("symbol") or "").upper(), currency), []).append(
            float(row["gross_pnl"])
        )

    strategies: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []
    for (sid, currency), bucket in sorted(grouped.items()):
        pnls = [float(item["gross_pnl"]) for item in bucket]
        holds = [
            float(item["holding_minutes"])
            for item in bucket
            if item.get("holding_minutes") is not None
        ]
        path_counts = {name: 0 for name in _PATH_ORDER}
        for item in bucket:
            name = str(item.get("path") or PATH_UNOBSERVED)
            path_counts[name] = path_counts.get(name, 0) + 1
        gross = round(sum(pnls), 4)
        expectancy = round(gross / len(pnls), 4) if pnls else None
        mean_hold = round(sum(holds) / len(holds), 2) if holds else None
        strategies.append(
            {
                "strategy_id": sid,
                "currency": currency,
                "closes": len(pnls),
                "wins": sum(1 for value in pnls if value > 0),
                "losses": sum(1 for value in pnls if value < 0),
                "scratches": sum(1 for value in pnls if value == 0),
                "gross_pnl": gross,
                "expectancy": expectancy,
                "mean_holding_minutes": mean_hold,
                "fees_known_closes": sum(1 for item in bucket if item.get("fees_known")),
                "path_counts": path_counts,
            }
        )
        detail = _sample_phrase(
            currency=currency,
            expectancy=expectancy,
            closes=len(pnls),
            mean_hold=mean_hold,
            path_counts=path_counts,
        )
        if len(pnls) < floor:
            action = "keep_running"
            detail = f"{detail}; {floor} comparable closes required before a draft"
        elif expectancy is not None and expectancy < 0:
            action = "draft_next_strategy"
        else:
            action = "keep_strategy"
        proposals.append(
            {
                "action": action,
                "strategy_id": sid,
                "currency": currency,
                "reason": detail,
                "applied": False,
            }
        )

    for cause, symbols in sorted(breaks.items()):
        proposals.append(
            {
                "action": "fix_execution",
                "cause": cause,
                "symbols": symbols,
                "closes": sum(
                    1
                    for row in rows
                    if row.get("entry_captured_at_open")
                    and str(row.get("execution_verdict") or "") == "broken"
                    and str(row.get("cause") or "") == cause
                ),
                "reason": "broken execution stays in the trial record and out of the method score",
                "applied": False,
            }
        )

    for (symbol, currency), pnls in sorted(faithful_by_symbol.items()):
        reason = entry_block_reason(pnls)
        if reason is None:
            continue
        proposals.append(
            {
                "action": "consider_symbol_block",
                "symbol": symbol,
                "currency": currency,
                "reason": reason,
                "applied": False,
            }
        )

    return {
        "strategy_version": STRATEGY_VERSION,
        "min_faithful_closes": floor,
        "trial_count": len(rows),
        "comparable_count": comparable,
        "strategy_close_count": comparable,
        "lesson_count": len(rows),
        "excluded": excluded,
        "by_strategy": strategies,
        "execution_breaks": [
            {"cause": cause, "symbols": symbols, "names": len(symbols)}
            for cause, symbols in sorted(breaks.items())
        ],
        "proposals": proposals,
        "auto_applied": False,
    }


def _sample_phrase(
    *,
    currency: str,
    expectancy: float | None,
    closes: int,
    mean_hold: float | None,
    path_counts: dict[str, int],
) -> str:
    hold = "hold unknown" if mean_hold is None else f"mean hold {mean_hold} min"
    paths = ", ".join(f"{name} {count}" for name, count in path_counts.items() if count)
    if not paths:
        paths = "path unobserved"
    return f"{currency} faithful expectancy {expectancy} over {closes} closes; {hold}; {paths}"


def _price(value: float | None) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0 or number != number:
        return None
    return number
