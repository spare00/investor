"""Turn an execution FIFO into per-currency closed trades.

Currencies are not summed. Unknown opening inventory is counted and omitted
from P&L. A missing fee is not subtracted as zero.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from app.intraday.pnl import FifoClose, FifoLedger, as_utc
from app.performance.entry_expectancy import compute_entry_reason_expectancy
from app.performance.trades import (
    ClosedTrade,
    compute_trade_metrics,
    group_trade_metrics_by_horizon,
)
from app.universe.entry_attribution import classify_exit_reason, read_attribution
from app.universe.horizons import UniverseHorizon


def _horizon(lc: Any, watchlist_hz: dict[str, str]) -> str:
    policy = dict(getattr(lc, "exit_policy", None) or {})
    raw = policy.get("horizon")
    if raw:
        try:
            return UniverseHorizon(str(raw).lower()).value
        except ValueError:
            pass
    symbol = str(getattr(lc, "symbol", "") or "").upper()
    if symbol in watchlist_hz:
        try:
            return UniverseHorizon(watchlist_hz[symbol].lower()).value
        except ValueError:
            return "unknown"
    return "unknown"


def _match_lifecycle(close: FifoClose, lifecycles: list[Any]) -> Any | None:
    if close.closed_at is None:
        return None
    closed = as_utc(close.closed_at)
    best: tuple[float, Any] | None = None
    for lc in lifecycles:
        if str(getattr(lc, "symbol", "") or "").upper() != close.symbol:
            continue
        venue = str(getattr(lc, "venue", "") or "").upper()
        if close.venue and venue and close.venue not in {"UNKNOWN"} and venue != close.venue:
            continue
        lc_closed = getattr(lc, "closed_at", None)
        if lc_closed is None:
            continue
        delta = abs((as_utc(lc_closed) - closed).total_seconds())
        if delta > 24 * 3600:
            continue
        if best is None or delta < best[0]:
            best = (delta, lc)
    return None if best is None else best[1]


def _closed_trade(
    close: FifoClose,
    lc: Any | None,
    watchlist_hz: dict[str, str],
) -> ClosedTrade:
    holding = 0.0
    if close.opened_at is not None and close.closed_at is not None:
        holding = (as_utc(close.closed_at) - as_utc(close.opened_at)).total_seconds() / 60.0
    horizon = "unknown"
    entry_timing = None
    entry_source = None
    trend = None
    exit_reason = None
    mfe_pct = None
    mae_pct = None
    notional = None
    risk = None
    if lc is not None:
        horizon = _horizon(lc, watchlist_hz)
        attr = read_attribution(lc)
        entry_timing = attr.get("entry_timing")
        entry_source = attr.get("entry_source")
        trend = attr.get("trend_at_entry")
        meta = dict(getattr(lc, "metadata_json", None) or {})
        exit_reason = classify_exit_reason(
            reason=str(meta.get("exit_reason") or ""),
            thesis=str((meta.get("exit_draft") or {}).get("reason") or ""),
            metadata=meta,
            horizon=horizon,
        )
        entry = float(getattr(lc, "average_entry_price", None) or 0)
        qty = abs(float(close.quantity))
        if entry > 0 and getattr(lc, "stop_price", None) and qty > 0:
            risk = abs(entry - float(lc.stop_price)) * qty
        if entry > 0 and qty > 0:
            notional = entry * qty
        policy = dict(getattr(lc, "exit_policy", None) or {})
        if entry > 0:
            peak = _float(policy.get("peak_price"))
            trough = _float(policy.get("trough_price"))
            if peak is not None:
                mfe_pct = max(0.0, (peak - entry) / entry)
            if trough is not None:
                mae_pct = max(0.0, (entry - trough) / entry)
    fees_known = close.fee is not None
    return ClosedTrade(
        pnl=float(close.gross_pnl or 0.0),
        holding_minutes=holding,
        risk_amount=risk,
        fees=float(close.fee or 0.0),
        fees_known=fees_known,
        symbol=close.symbol,
        horizon=horizon,
        entry_timing=entry_timing,
        entry_source=entry_source,
        trend_at_entry=trend,
        exit_reason=exit_reason,
        mfe_pct=mfe_pct,
        mae_pct=mae_pct,
        notional=notional,
        currency=close.currency or "UNKNOWN",
    )


def _float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None


def books_from_ledger(
    ledger: FifoLedger,
    *,
    lifecycles: list[Any],
    watchlist_hz: dict[str, str],
    period_start: datetime,
    period_end: datetime,
) -> dict[str, Any]:
    """Per-currency metrics. The top level is not a cross-currency sum."""
    start = as_utc(period_start)
    end = as_utc(period_end)
    grouped: dict[str, list[ClosedTrade]] = defaultdict(list)
    unknown: dict[str, int] = defaultdict(int)
    for book in ledger.books:
        for close in book.closes:
            if close.closed_at is None:
                continue
            closed = as_utc(close.closed_at)
            if not (start <= closed <= end):
                continue
            currency = close.currency or "UNKNOWN"
            if close.gross_pnl is None:
                unknown[currency] += 1
                continue
            lc = _match_lifecycle(close, lifecycles)
            grouped[currency].append(_closed_trade(close, lc, watchlist_hz))

    by_currency: dict[str, Any] = {}
    for currency in sorted(set(grouped) | set(unknown)):
        trades = grouped.get(currency, [])
        fees_known = all(trade.fees_known for trade in trades) if trades else False
        treatment = "net" if fees_known else "gross_fees_unrecorded"
        metrics = compute_trade_metrics(trades, method="execution_fifo_gross")
        metrics["by_horizon"] = group_trade_metrics_by_horizon(
            trades, method="execution_fifo_gross"
        )
        metrics.update(compute_entry_reason_expectancy(trades))
        metrics["fee_treatment"] = treatment
        metrics["unknown_basis_count"] = int(unknown.get(currency, 0))
        metrics["currency"] = currency
        by_currency[currency] = metrics

    return {
        "unit": "execution_fifo",
        "currency_note": (
            "Closed trades are FIFO from executions and are not summed across currencies. "
            "Dividends, splits, FX, and cash transfers are not in this book. "
            "A round trip with no opening lot is counted as unknown basis, not as zero P&L."
        ),
        "unobserved_adjustments": list(ledger.unobserved_adjustments),
        "unknown_basis_count": int(sum(unknown.values())),
        "skipped_fill_count": int(ledger.skipped_fills),
        "by_currency": by_currency,
        **compute_trade_metrics([], method="execution_fifo_gross"),
    }
