"""Compact, decision-first user briefs for local (and cloud) LLM agents.

Full Pydantic dumps confuse small models and blow the context window.
Each brief is: one question, objective numbers, a JSON answer contract.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.schemas.cio import CIOInput
from app.schemas.devils_advocate import DevilsAdvocateInput
from app.schemas.macro_strategist import MacroStrategistInput
from app.schemas.market_intelligence import MarketIntelligenceInput, MarketIntelligenceOutput
from app.schemas.quant_strategist import BarSnapshot, QuantStrategistInput, QuantStrategistOutput
from app.schemas.risk_manager import RiskManagerInput, RiskManagerOutput
from app.schemas.universe_manager import UniverseManagerInput


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _compact(data: Any) -> str:
    return json.dumps(data, default=str, separators=(",", ":"), ensure_ascii=True)


def _clip_obj(value: Any, *, n: int = 6) -> Any:
    if isinstance(value, dict):
        return dict(list(value.items())[:n])
    if isinstance(value, list):
        return value[:n]
    if isinstance(value, str):
        return value[:200]
    return value


def _drop_empty(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        if value is None or value == "" or value == [] or value == {}:
            continue
        out[key] = value
    return out


def _watch_by_book(rows: list[dict] | None, *, limit: int = 16) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {"scalp": [], "day": [], "short": []}
    for raw in (rows or [])[:limit]:
        if not isinstance(raw, dict):
            continue
        sym = str(raw.get("symbol") or "").upper()
        if not sym:
            continue
        hz = str(raw.get("horizon") or "").strip().lower()
        if hz in grouped and len(grouped[hz]) < 8:
            grouped[hz].append(sym)
    return {k: v for k, v in grouped.items() if v}


def _watch_rows(rows: list[dict] | None, *, limit: int = 16) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in (rows or [])[:limit]:
        if not isinstance(raw, dict):
            continue
        out.append(
            _drop_empty(
                {
                    "s": str(raw.get("symbol") or "").upper(),
                    "h": raw.get("horizon"),
                    "p": raw.get("priority"),
                    "st": raw.get("status"),
                }
            )
        )
    return [r for r in out if r.get("s")]


def _bar_row(bar: BarSnapshot) -> dict[str, Any]:
    return _drop_empty(
        {
            "s": bar.symbol.upper(),
            "last": bar.last,
            "rsi": bar.rsi_14,
            "atr": bar.atr_14,
            "sma50": bar.sma_50,
            "sma200": bar.sma_200,
            "vol": bar.volume,
            "gap": bar.gap_pct if bar.gap_pct is not None else bar.premarket_change_pct,
            "bid": bar.bid,
            "ask": bar.ask,
        }
    )


def _mi_summary(mi: MarketIntelligenceOutput | None) -> dict[str, Any]:
    if mi is None:
        return {}
    events = []
    for ev in (mi.market_events or [])[:6]:
        events.append(
            _drop_empty(
                {
                    "h": ev.headline[:160],
                    "imp": ev.importance,
                    "sent": getattr(ev.sentiment, "value", ev.sentiment),
                    "sym": ev.symbols[:4],
                }
            )
        )
    return _drop_empty(
        {
            "quality": mi.data_quality_score,
            "themes": (mi.top_market_themes or [])[:5],
            "events": events,
            "missing": (mi.missing_information or [])[:4],
        }
    )


def mi_summary_for_downstream(mi: MarketIntelligenceOutput | None) -> dict[str, Any]:
    """Python-owned compact MI blob for Macro/Quant — never a full model dump."""
    return _mi_summary(mi)


def _quant_summary(quant: QuantStrategistOutput | None) -> dict[str, Any]:
    if quant is None:
        return {}
    views = []
    for view in (quant.symbol_views or [])[:10]:
        ez = view.entry_zone
        views.append(
            _drop_empty(
                {
                    "s": view.symbol.upper(),
                    "trend": getattr(view.trend_state, "value", view.trend_state),
                    "mom": getattr(view.momentum_state, "value", view.momentum_state),
                    "vol": getattr(view.volatility_state, "value", view.volatility_state),
                    "p": view.probability_estimate,
                    "stop": view.stop_or_invalidation,
                    "entry": [ez.min, ez.max] if ez is not None else None,
                }
            )
        )
    return _drop_empty(
        {
            "trend": getattr(quant.market_trend_state, "value", quant.market_trend_state),
            "mom": getattr(quant.market_momentum_state, "value", quant.market_momentum_state),
            "vol": getattr(quant.market_volatility_state, "value", quant.market_volatility_state),
            "breadth": getattr(quant.market_breadth_state, "value", quant.market_breadth_state),
            "views": views,
        }
    )


def _risk_summary(risk: RiskManagerOutput | None) -> dict[str, Any]:
    if risk is None:
        return {}
    return _drop_empty(
        {
            "verdict": getattr(risk.overall_verdict, "value", risk.overall_verdict),
            "vetoes": list(risk.hard_vetoes or [])[:8],
            "halt": bool(risk.halt_new_trades),
            "cash_pct": risk.cash_pct,
            "gross_pct": risk.gross_exposure_pct,
            "warn": list(risk.soft_warnings or [])[:4],
        }
    )


def _ask(question: str, data: Any, answer: str) -> str:
    return (
        f"QUESTION: {question}\n"
        f"DATA: {_compact(data)}\n"
        f"ANSWER: {answer} JSON only. Short strings. Decide now."
    )


def market_intelligence_brief(payload: MarketIntelligenceInput) -> str:
    news = []
    for item in payload.news_items[:12]:
        news.append(
            _drop_empty(
                {
                    "h": item.headline[:180],
                    "src": item.source,
                    "at": _iso(item.published_at),
                    "sym": [s.upper() for s in item.symbols[:6]],
                }
            )
        )
    data = {
        "as_of": _iso(payload.as_of),
        "held": [s.upper() for s in payload.portfolio_symbols[:16]],
        "allow": [s.upper() for s in payload.allowlist[:16]],
        "watch": _watch_rows(payload.watchlist),
        "news": news,
        "n_news": len(payload.news_items),
        "earnings_n": len(payload.earnings_summaries or []),
        "filings_n": len(payload.sec_filings or []),
    }
    return _ask(
        "Which facts matter for this book? Cluster duplicates. No trade advice.",
        data,
        "MarketIntelligenceOutput. <=8 market_events. facts one line each.",
    )


def macro_brief(payload: MacroStrategistInput) -> str:
    m = payload.macro
    data = _drop_empty(
        {
            "as_of": _iso(payload.as_of),
            "fed": m.fed_funds_rate,
            "cpi": m.cpi_yoy,
            "pce": m.pce_yoy,
            "unemp": m.unemployment_rate,
            "gdp": m.gdp_growth_q_o_q,
            "y10": m.us_10y_yield,
            "y2": m.us_2y_yield,
            "dxy": m.dxy,
            "wti": m.wti_oil,
            "gold": m.gold,
            "hy_bps": m.hy_credit_spread_bps,
            "geo": (payload.geopolitical_events or [])[:4],
            "themes": _clip_obj(payload.market_intelligence_summary),
        }
    )
    return _ask(
        "Pick exactly one market_regime from the numbers. Confidence 0-1.",
        data,
        "MacroStrategistOutput. <=3 bullish_factors, <=3 bearish_factors, <=3 invalidation_conditions.",
    )


def quant_brief(payload: QuantStrategistInput) -> str:
    data = {
        "as_of": _iso(payload.as_of),
        "vix": payload.vix,
        "ad": payload.advance_decline,
        "index": [_bar_row(b) for b in payload.index_bars[:6]],
        "symbols": [_bar_row(b) for b in (payload.symbol_bars or payload.index_bars)[:16]],
        "watch": _watch_rows(payload.watchlist),
        "books": _watch_by_book(payload.watchlist),
        "playbooks": [
            {"h": "scalp", "ko": "초단타", "rule": "continuation only, tight stop, no overnight"},
            {"h": "day", "ko": "단타", "rule": "session structure, flatten before close"},
            {"h": "short", "ko": "단기", "rule": "swing trend, wider stop, overnight ok"},
        ],
        "themes": _clip_obj(payload.market_intelligence_summary),
    }
    return _ask(
        "From these bars only: market trend and per-symbol trend/momentum/stop. Apply the matching book playbook (scalp/day/short). Ignore medium. No invented indicators.",
        data,
        "QuantStrategistOutput. Stop from ATR/horizon policy. p from the numbers. <=12 symbol_views.",
    )


def risk_brief(payload: RiskManagerInput, engine_preview: dict[str, Any] | None = None) -> str:
    port = payload.portfolio
    pos = [
        _drop_empty(
            {
                "s": p.symbol.upper(),
                "qty": p.quantity,
                "w": round(p.weight_pct, 2),
                "upnl": round(p.unrealized_pnl, 2),
                "venue": p.venue,
            }
        )
        for p in (port.positions or [])[:12]
    ]
    trades = [
        _drop_empty(
            {
                "s": t.symbol.upper(),
                "side": t.side,
                "qty": t.quantity,
                "px": t.entry_price,
                "stop": t.stop_loss,
            }
        )
        for t in (payload.proposed_trades or [])[:8]
    ]
    data = {
        "engine": engine_preview or {},
        "portfolio": {
            "eq": port.equity,
            "cash_pct": port.cash_pct,
            "gross_pct": port.gross_exposure_pct,
            "dd": port.drawdown_pct,
            "day_pnl_pct": port.daily_pnl_pct,
            "halted": port.trading_halted,
        },
        "positions": pos,
        "proposed": trades,
        "dq": payload.data_quality_score,
        "session_clear": payload.market_session_clear,
        "live_px": {
            "required": payload.live_prices_required,
            "live": payload.price_feed_live,
            "providers": payload.price_providers[:4],
        },
        "regime": getattr(payload.macro.market_regime, "value", None) if payload.macro else None,
        "themes": (payload.market_intelligence.top_market_themes[:4] if payload.market_intelligence else []),
    }
    return _ask(
        "Engine Hard Vetoes already stand. Name at most 3 extra soft risks, or [].",
        data,
        "RiskManagerOutput. Echo engine verdicts. soft_warnings only for new issues. No data_quality_score.",
    )


def devil_brief(payload: DevilsAdvocateInput) -> str:
    theses = [
        _drop_empty(
            {
                "s": (t.symbol or "").upper() or None,
                "dir": t.direction,
                "sum": t.summary[:160],
            }
        )
        for t in (payload.proposed_theses or [])[:6]
    ]
    data = {
        "as_of": _iso(payload.as_of),
        "theses": theses or ["none"],
        "consensus": payload.consensus_lean,
        "mi": _mi_summary(payload.market_intelligence),
        "regime": getattr(payload.macro.market_regime, "value", None) if payload.macro else None,
        "macro_conf": payload.macro.confidence if payload.macro else None,
        "quant": _quant_summary(payload.quant),
        "risk": _risk_summary(payload.risk),
        "watch": _watch_rows(payload.watchlist, limit=8),
    }
    return _ask(
        "Is the thesis already priced in? prefer_no_trade true or false. One strongest counterpoint.",
        data,
        "DevilsAdvocateOutput. Booleans as true/false. Strings <=140 chars.",
    )


def cio_brief(payload: CIOInput) -> str:
    positions = [
        _drop_empty(
            {
                "s": p.symbol.upper(),
                "qty": p.quantity,
                "w": round(p.weight_pct, 2),
                "upnl": round(p.unrealized_pnl, 2),
                "venue": p.venue,
            }
        )
        for p in (payload.positions or [])[:16]
    ]
    data = {
        "as_of": _iso(payload.as_of),
        "cash_pct": payload.portfolio_cash_pct,
        "positions": positions or ["FLAT"],
        "allow": [s.upper() for s in payload.allowlist[:16]],
        "watch": _watch_rows(payload.watchlist),
        "books": _watch_by_book(payload.watchlist),
        "playbooks": [
            {"h": "scalp", "ko": "초단타", "rule": "tape follow, no overnight, cut on exhaustion"},
            {"h": "day", "ko": "단타", "rule": "session trade, flatten into close"},
            {"h": "short", "ko": "단기", "rule": "swing hold, reduce on exhaustion, sell on trend break"},
        ],
        "mi": _mi_summary(payload.market_intelligence),
        "macro": _drop_empty(
            {
                "regime": getattr(payload.macro.market_regime, "value", payload.macro.market_regime),
                "conf": payload.macro.confidence,
                "bull": (payload.macro.bullish_factors or [])[:3],
                "bear": (payload.macro.bearish_factors or [])[:3],
            }
        ),
        "quant": _quant_summary(payload.quant),
        "risk": _risk_summary(payload.risk),
        "devil": _drop_empty(
            {
                "no_trade": payload.devil.prefer_no_trade,
                "why": (payload.devil.prefer_no_trade_rationale or "")[:160],
                "priced_in": payload.devil.information_already_in_price,
                "score": payload.devil.challenge_score,
                "rec": getattr(payload.devil.recommendation, "value", payload.devil.recommendation),
            }
        ),
    }
    return _ask(
        "Decide per book. 초단타/단타/단기 each follow their playbook — never one strategy for all. Ignore medium. One portfolio_action. Review every open position in THIS book's allowlist. New buys only from allowlist if risk_approval.",
        data,
        "CIODecision. HONOR hard vetoes. Entries need numeric stop_loss. thesis/invalidation <=80 chars.",
    )


def universe_brief(payload: UniverseManagerInput) -> str:
    watch = _watch_rows(payload.current_watchlist, limit=24)
    data = {
        "as_of": _iso(payload.as_of),
        "venues": payload.enabled_venues,
        "held": [h.upper() for h in payload.holdings[:16]],
        "watch": watch,
        "seed": [s.upper() for s in payload.seed_pool[:20]],
        "seed_by_venue": {
            k: [x.upper() for x in v[:12]] for k, v in (payload.seed_pool_by_venue or {}).items()
        },
        "candidates": [s.upper() for s in payload.candidate_pool[:20]],
        "regime": payload.market_regime,
        "themes": (payload.themes or [])[:6],
        "limits": {"watch": payload.watchlist_limit, "focus": payload.focus_limit},
        "outcomes": payload.recent_outcomes or {},
    }
    return _ask(
        "Keep/pause/add liquid names only. Focus <=limit. Cover both venues if enabled. No obscure tickers.",
        data,
        "UniverseManagerOutput. thesis/invalidation <=80 chars.",
    )


def dump_model_without_trace(model: BaseModel) -> str:
    """Fallback compact dump if a caller still needs a model blob."""
    payload = model.model_dump(mode="json", exclude={"trace"}, exclude_none=True)
    return _compact(payload)
