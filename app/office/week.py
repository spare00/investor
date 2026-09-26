"""Last-week CIO plans and closes for office gossip. Not on the trading path."""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.intraday.pnl import lifecycle_pnl
from app.models import CIODecisionRecord, PositionLifecycle

_BUYISH = frozenset({"BUY", "STRONG_BUY", "SCALE_IN"})
_CASHISH = frozenset({"NO_TRADE", "HOLD", "STAY_CASH", "CASH", "REDUCE", "SCALE_OUT"})
_SELLISH = frozenset({"SELL", "PARTIAL_SELL", "REDUCE", "SCALE_OUT"})
_TICKER = re.compile(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$")
_TTL_SECONDS = 8 * 60

_cache: dict[str, Any] = {}
_cache_at = 0.0


def reset_office_week_for_tests() -> None:
    global _cache, _cache_at
    _cache = {}
    _cache_at = 0.0


def _names(values: list[Any], *, limit: int = 8) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        sym = str(raw or "").upper().strip()
        if not sym or len(sym) > 6 or not _TICKER.fullmatch(sym):
            continue
        if sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
        if len(out) >= limit:
            break
    return out


def _plan_action(plan: dict[str, Any]) -> str:
    raw = plan.get("action")
    return str(getattr(raw, "value", raw) or "").upper()


def week_review_from_records(
    *,
    decisions: list[Any],
    closes: list[Any],
) -> dict[str, Any]:
    """Pure clip dict from already-loaded rows — no extra I/O."""
    bought: list[str] = []
    sold: list[str] = []
    blocked: list[str] = []
    suggested: list[str] = []
    regimes: list[str] = []
    for rec in decisions:
        payload = rec.payload if isinstance(getattr(rec, "payload", None), dict) else {}
        pa = str(getattr(rec, "portfolio_action", "") or "").upper()
        why = str(
            getattr(rec, "reason_not_to_trade", None) or payload.get("reason_not_to_trade") or ""
        )
        risk_ok = bool(getattr(rec, "risk_approval", True))
        regime = str(getattr(rec, "market_regime", "") or "")
        if regime and regime not in regimes:
            regimes.append(regime)
        halted = pa in _CASHISH or bool(why) or not risk_ok
        plans = payload.get("symbol_actions") or []
        if not isinstance(plans, list):
            continue
        for plan in plans:
            if not isinstance(plan, dict):
                continue
            sym = str(plan.get("symbol") or "").upper()
            act = _plan_action(plan)
            if act in _BUYISH:
                suggested.append(sym)
                if halted:
                    blocked.append(sym)
                else:
                    bought.append(sym)
            elif act in _SELLISH:
                sold.append(sym)

    winners: list[dict[str, Any]] = []
    losers: list[dict[str, Any]] = []
    by_symbol: dict[str, float] = {}
    n_closes = 0
    for lc in closes:
        pnl = float(lifecycle_pnl(lc) or 0.0)
        sym = str(getattr(lc, "symbol", "") or "").upper()
        if not sym:
            continue
        n_closes += 1
        by_symbol[sym] = by_symbol.get(sym, 0.0) + pnl
    for sym, pnl in by_symbol.items():
        row = {"s": sym, "pnl": round(pnl, 0)}
        if pnl >= 0:
            winners.append(row)
        else:
            losers.append(row)
    winners.sort(key=lambda r: abs(float(r["pnl"])), reverse=True)
    losers.sort(key=lambda r: abs(float(r["pnl"])), reverse=True)
    total = sum(by_symbol.values())
    return {
        "bought": _names(bought),
        "sold": _names(sold),
        "blocked": _names(blocked),
        "suggested": _names(suggested),
        "winners": winners[:4],
        "losers": losers[:4],
        "regimes": regimes[:3],
        "pnl": round(total, 0),
        "n_closes": n_closes,
        "n_decisions": len(decisions),
    }


async def load_office_week(session: AsyncSession, *, days: int = 7) -> dict[str, Any]:
    """Two small lookbacks, cached a few minutes. Skip-safe."""
    global _cache, _cache_at
    now_m = time.monotonic()
    if _cache and now_m - _cache_at < _TTL_SECONDS:
        return _cache
    end = datetime.now(UTC)
    start = end - timedelta(days=max(2, int(days)))
    try:
        decisions = list(
            (
                await session.execute(
                    select(CIODecisionRecord)
                    .where(CIODecisionRecord.decision_timestamp >= start)
                    .order_by(desc(CIODecisionRecord.decision_timestamp))
                    .limit(24)
                )
            )
            .scalars()
            .all()
        )
        closes = list(
            (
                await session.execute(
                    select(PositionLifecycle)
                    .where(
                        PositionLifecycle.status == "CLOSED",
                        PositionLifecycle.closed_at.is_not(None),
                        PositionLifecycle.closed_at >= start,
                    )
                    .order_by(desc(PositionLifecycle.closed_at))
                    .limit(48)
                )
            )
            .scalars()
            .all()
        )
    except Exception:  # noqa: BLE001 — floor toy must not break summary
        return _cache or {}
    data = week_review_from_records(decisions=decisions, closes=closes)
    _cache = data
    _cache_at = now_m
    return data
