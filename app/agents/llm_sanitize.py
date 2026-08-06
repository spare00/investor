"""Normalize messy LLM JSON before Pydantic validation."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from app.schemas.common import (
    BreadthState,
    LiquidityState,
    MarketRegime,
    MomentumState,
    NewsCategory,
    OrderType,
    PortfolioAction,
    RiskVerdict,
    Sentiment,
    SymbolAction,
    TimeHorizon,
    TrendState,
    VolatilityState,
)

_ENUM_SYNONYMS: dict[type[StrEnum], dict[str, str]] = {
    Sentiment: {
        "pos": "positive",
        "neg": "negative",
        "bullish": "positive",
        "bearish": "negative",
    },
    NewsCategory: {
        "monetary policy": "fed",
        "monetary_policy": "fed",
        "fed policy": "fed",
        "central bank": "fed",
        "market performance": "macro",
        "markets": "macro",
        "economy": "macro",
        "economic": "macro",
        "politics": "geopolitics",
        "company": "corporate",
        "m&a": "corporate",
        "sec": "regulatory",
        "regulation": "regulatory",
    },
    TrendState: {
        "bullish": "up",
        "bearish": "down",
        "neutral": "sideways",
        "flat": "sideways",
        "range": "sideways",
        "ranging": "sideways",
    },
    MomentumState: {
        "neutral": "steady",
        "strong": "accelerating",
        "weak": "decelerating",
        "fading": "exhausted",
    },
    VolatilityState: {
        "moderate": "normal",
        "medium": "normal",
        "high": "elevated",
        "very high": "extreme",
        "very_high": "extreme",
    },
    BreadthState: {
        "neutral": "mixed",
        "ok": "healthy",
        "poor": "weak",
        "bad": "deteriorating",
    },
    LiquidityState: {
        "adequate": "normal",
        "good": "ample",
        "poor": "tight",
        "bad": "stressed",
        "moderate": "normal",
    },
    MarketRegime: {
        "risk on": "RISK_ON",
        "risk-on": "RISK_ON",
        "risk_off": "RISK_OFF",
        "risk off": "RISK_OFF",
        "risk-off": "RISK_OFF",
        "bullish": "RISK_ON",
        "bearish": "RISK_OFF",
    },
}

_ENUM_TYPES: tuple[type[StrEnum], ...] = (
    Sentiment,
    NewsCategory,
    MarketRegime,
    TrendState,
    MomentumState,
    VolatilityState,
    BreadthState,
    LiquidityState,
    RiskVerdict,
    PortfolioAction,
    SymbolAction,
    OrderType,
    TimeHorizon,
)

_FIELD_ALIASES: dict[str, str] = {
    "trend": "trend_state",
    "momentum": "momentum_state",
    "volatility": "volatility_state",
    "breadth": "breadth_state",
    "liquidity": "liquidity_state",
    "market_trend": "market_trend_state",
    "market_momentum": "market_momentum_state",
    "market_volatility": "market_volatility_state",
    "market_breadth": "market_breadth_state",
    "market_liquidity": "market_liquidity_state",
    "upside": "upside_scenario",
    "downside": "downside_scenario",
    "sector_impacts": "expected_sector_impact",
    "sector_impact": "expected_sector_impact",
}

_BOOL_FIELDS = {
    "information_already_in_price",
    "prefer_no_trade",
    "crowd_trade_risk",
    "hedge_required",
    "risk_approval",
    "hard_veto_honored",
    "halt_new_trades",
}

_SCORE_FIELDS = {
    "data_quality_score",
    "challenge_score",
    "confidence",
    "probability_estimate",
    "probability",
}


def coerce_enum_value(enum_cls: type[StrEnum], value: Any) -> Any:
    if isinstance(value, enum_cls):
        return value
    if not isinstance(value, str):
        return value
    raw = value.strip()
    if not raw:
        return value
    try:
        return enum_cls(raw)
    except ValueError:
        pass
    lower = raw.lower()
    by_value = {e.value.lower(): e for e in enum_cls}
    if lower in by_value:
        return by_value[lower]
    by_name = {e.name.lower(): e for e in enum_cls}
    if lower in by_name:
        return by_name[lower]
    syn = _ENUM_SYNONYMS.get(enum_cls, {}).get(lower)
    if syn is not None:
        return enum_cls(syn)
    if enum_cls is NewsCategory:
        return NewsCategory.OTHER
    return value


def _clamp_unit(value: Any, *, percent_ok: bool = False) -> Any:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return value
    if num > 1.0:
        if percent_ok and num <= 100.0:
            num = num / 100.0
        else:
            num = 1.0
    return max(0.0, min(1.0, num))


def _unwrap_bool(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        for key in ("value", "flag", "result", "bool", "is_true", "answer"):
            if key in value and isinstance(value[key], bool):
                return value[key]
        # Nested prose without explicit bool — conservative True if content present.
        return True
    if isinstance(value, str):
        lower = value.strip().lower()
        if lower in {"true", "yes", "y", "1"}:
            return True
        if lower in {"false", "no", "n", "0"}:
            return False
    return value


def _extract_bool_rationale(node: dict[str, Any], bool_key: str, rationale_key: str) -> None:
    raw = node.get(bool_key)
    if isinstance(raw, dict):
        if rationale_key not in node:
            node[rationale_key] = _as_text(raw)
        node[bool_key] = _unwrap_bool(raw)


def _normalize_price_zone(value: Any) -> Any:
    if isinstance(value, dict) and "min" in value and "max" in value:
        return value
    if isinstance(value, (int, float)):
        px = float(value)
        return {"min": px * 0.99, "max": px * 1.01}
    if isinstance(value, list) and len(value) >= 2:
        try:
            return {"min": float(value[0]), "max": float(value[1])}
        except (TypeError, ValueError):
            return None
    return value


def _normalize_scenario(value: Any) -> Any:
    if value is None:
        return value
    if isinstance(value, dict):
        out = dict(value)
        if "target" in out and "target_price" not in out:
            out["target_price"] = out.pop("target")
        out.setdefault("name", "scenario")
        out.setdefault("description", out.get("summary") or out.get("name") or "scenario")
        if "probability" in out:
            out["probability"] = _clamp_unit(out["probability"], percent_ok=True)
        else:
            out["probability"] = 0.5
        # Drop unknown keys that StrictModel would reject.
        keep = {"name", "description", "probability", "target_price"}
        return {k: v for k, v in out.items() if k in keep}
    if isinstance(value, str):
        return {"name": "scenario", "description": value, "probability": 0.5}
    if isinstance(value, (int, float)):
        return {
            "name": "scenario",
            "description": "LLM numeric scenario",
            "probability": 0.5,
            "target_price": float(value),
        }
    return value


def _as_text(value: Any) -> Any:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("description", "rationale", "reason", "summary", "text", "name"):
            if isinstance(value.get(key), str) and value[key].strip():
                return value[key]
        return json_dumps_safe(value)
    if isinstance(value, list):
        return "; ".join(str(x) for x in value)
    return value


def json_dumps_safe(value: Any) -> str:
    try:
        import json

        return json.dumps(value, default=str)
    except Exception:  # noqa: BLE001
        return str(value)


def _fill_symbol_action_defaults(plan: dict[str, Any]) -> dict[str, Any]:
    plan.setdefault("confidence", 50)
    plan.setdefault("target_position_pct", 0.0)
    plan.setdefault("thesis", plan.get("reason") or plan.get("summary") or "LLM plan")
    plan.setdefault("invalidation", plan.get("stop_condition") or "n/a")
    plan.setdefault("order_type", "limit")
    plan.setdefault("time_horizon", "intraday")
    plan.setdefault("take_profit", [])
    if "entry_zone" in plan:
        plan["entry_zone"] = _normalize_price_zone(plan["entry_zone"])
    # confidence sometimes 0-1
    conf = plan.get("confidence")
    if isinstance(conf, float) and 0.0 <= conf <= 1.0:
        plan["confidence"] = int(round(conf * 100))
    return plan


def _split_scenarios_list(view: dict[str, Any]) -> None:
    """Map LLM ``scenarios`` list/dict onto upside/downside_scenario keys."""
    raw = view.pop("scenarios", None)
    if raw is None:
        return
    upside = view.get("upside_scenario")
    downside = view.get("downside_scenario")
    items: list[Any]
    if isinstance(raw, dict):
        items = list(raw.values()) if raw else []
        if "upside" in raw or "downside" in raw:
            if upside is None and raw.get("upside") is not None:
                view["upside_scenario"] = _normalize_scenario(raw.get("upside"))
            if downside is None and raw.get("downside") is not None:
                view["downside_scenario"] = _normalize_scenario(raw.get("downside"))
            return
    elif isinstance(raw, list):
        items = raw
    else:
        items = [raw]
    for item in items:
        scen = _normalize_scenario(item)
        if not isinstance(scen, dict):
            continue
        name = str(scen.get("name") or "").lower()
        desc = str(scen.get("description") or "").lower()
        blob = f"{name} {desc}"
        if upside is None and any(t in blob for t in ("up", "bull", "rally", "positive")):
            view["upside_scenario"] = scen
            upside = scen
        elif downside is None and any(t in blob for t in ("down", "bear", "sell", "negative")):
            view["downside_scenario"] = scen
            downside = scen
        elif upside is None:
            view["upside_scenario"] = scen
            upside = scen
        elif downside is None:
            view["downside_scenario"] = scen
            downside = scen


def _normalize_sector_impact(value: Any) -> Any:
    if value is None:
        return []
    if isinstance(value, list):
        out: list[Any] = []
        for item in value:
            if isinstance(item, dict):
                row = {
                    "sector": str(item.get("sector") or item.get("name") or "unknown"),
                    "bias": str(item.get("bias") or item.get("direction") or "neutral"),
                    "rationale": str(
                        item.get("rationale") or item.get("reason") or item.get("summary") or ""
                    ),
                }
                out.append(row)
            elif isinstance(item, str):
                out.append({"sector": item, "bias": "neutral", "rationale": ""})
        return out
    if isinstance(value, dict):
        # {"technology": "Potential ...", ...} or already SectorImpact-shaped
        if {"sector", "bias"} <= set(value.keys()):
            return [
                {
                    "sector": str(value.get("sector") or "unknown"),
                    "bias": str(value.get("bias") or "neutral"),
                    "rationale": str(value.get("rationale") or ""),
                }
            ]
        return [
            {
                "sector": str(k),
                "bias": "neutral",
                "rationale": v if isinstance(v, str) else _as_text(v),
            }
            for k, v in value.items()
        ]
    return value


def _agent_shape_fixes(out: dict[str, Any]) -> dict[str, Any]:
    """Coerce common LLM shape mistakes that StrictModel would reject."""
    looks_mi = "market_events" in out or "top_market_themes" in out
    looks_macro = "expected_sector_impact" in out or "bullish_factors" in out or "market_regime" in out
    looks_quant = "symbol_views" in out or "market_trend_state" in out
    looks_risk = "overall_verdict" in out or "hard_vetoes" in out or "trade_adjustments" in out
    looks_devil = (
        "strongest_reason_thesis_is_wrong" in out
        or "challenge_score" in out
        or "opposing_market_scenario" in out
    )

    if looks_mi and "data_quality_score" not in out:
        missing = out.get("missing_information") or []
        out["data_quality_score"] = 0.3 if missing else 0.5

    if "expected_sector_impact" in out:
        out["expected_sector_impact"] = _normalize_sector_impact(out["expected_sector_impact"])

    if looks_quant and isinstance(out.get("symbol_views"), list):
        for view in out["symbol_views"]:
            if isinstance(view, dict):
                _split_scenarios_list(view)
                for scen_key in ("upside_scenario", "downside_scenario"):
                    if scen_key in view:
                        view[scen_key] = _normalize_scenario(view[scen_key])

    if looks_risk:
        # Input-only / prompt-echo field — not on RiskManagerOutput.
        out.pop("data_quality_score", None)
        out.setdefault("cash_pct", 50.0)
        out.setdefault("gross_exposure_pct", 0.0)
        out.setdefault("notes", [])
        if isinstance(out.get("notes"), str):
            out["notes"] = [out["notes"]]

    if looks_devil:
        if "prefer_no_trade" not in out:
            rec = str(out.get("recommendation") or "").upper()
            try:
                challenge = float(out.get("challenge_score") or 0.0)
            except (TypeError, ValueError):
                challenge = 0.0
            out["prefer_no_trade"] = rec in {"WAIT", "NO_TRADE"} or challenge >= 0.7
        out.setdefault("prefer_no_trade_rationale", "")
        if "information_already_in_price" not in out:
            out["information_already_in_price"] = False
        out.setdefault("information_already_in_price_rationale", "")
        if "challenge_score" not in out:
            out["challenge_score"] = 0.5
        if "opposing_market_scenario" in out:
            out["opposing_market_scenario"] = _as_text(out["opposing_market_scenario"])

    if looks_macro and "data_quality_score" not in out and "market_regime" in out:
        out.setdefault("data_quality_score", 0.5)

    return out


def sanitize_llm_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Best-effort cleanup so StrictModel validation succeeds more often."""

    def walk(node: Any, key: str | None = None) -> Any:
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for k, v in node.items():
                nk = _FIELD_ALIASES.get(k, k)
                if nk in out and k != nk:
                    continue
                out[nk] = walk(v, nk)
            if key is None or key in {"", "root"}:
                out.setdefault("timestamp", datetime.now(UTC).isoformat())
            if "cash_target_pct" in out:
                try:
                    out["cash_target_pct"] = max(0.0, min(100.0, float(out["cash_target_pct"])))
                except (TypeError, ValueError):
                    pass
            if "symbol_actions" in out and isinstance(out["symbol_actions"], list):
                out["symbol_actions"] = [
                    _fill_symbol_action_defaults(x) if isinstance(x, dict) else x
                    for x in out["symbol_actions"]
                ]
            if "entry_zone" in out:
                out["entry_zone"] = _normalize_price_zone(out["entry_zone"])
            for scen_key in ("upside_scenario", "downside_scenario"):
                if scen_key in out:
                    out[scen_key] = _normalize_scenario(out[scen_key])
            if "scenarios" in out and (
                "upside_scenario" not in out or "downside_scenario" not in out
            ):
                _split_scenarios_list(out)
            _extract_bool_rationale(
                out, "information_already_in_price", "information_already_in_price_rationale"
            )
            _extract_bool_rationale(out, "prefer_no_trade", "prefer_no_trade_rationale")
            if "opposing_market_scenario" in out:
                out["opposing_market_scenario"] = _as_text(out["opposing_market_scenario"])
            if "crowd_trade_risk" in out and not isinstance(out["crowd_trade_risk"], bool):
                # Sometimes LLM returns prose instead of bool.
                text = _as_text(out["crowd_trade_risk"])
                out["crowd_trade_risk"] = bool(text) and text.lower() not in {"false", "none", "no"}
                if "trap_risk" not in out or not out["trap_risk"]:
                    out.setdefault("missing_information", [])
            return out
        if isinstance(node, list):
            return [walk(x, key) for x in node]
        if key in _BOOL_FIELDS:
            return _unwrap_bool(node)
        if key in _SCORE_FIELDS or (key and key.endswith("_score")):
            # confidence on SymbolActionPlan is 0-100 int — handled in defaults.
            if key == "confidence" and isinstance(node, (int, float)) and node > 1:
                return node
            # Probabilities may arrive as 0-100; quality scores >1 are clamped to 1.
            percent_ok = key in {"probability_estimate", "probability", "confidence"}
            return _clamp_unit(node, percent_ok=percent_ok)
        if key and isinstance(node, str):
            for enum_cls in _ENUM_TYPES:
                # Heuristic: only coerce when field name suggests the enum.
                name = enum_cls.__name__.replace("State", "").lower()
                kn = key.lower()
                if (
                    kn == enum_cls.__name__.lower()
                    or kn.endswith(enum_cls.__name__.lower())
                    or name in kn
                    or kn in {
                        "category",
                        "sentiment",
                        "market_regime",
                        "overall_verdict",
                        "portfolio_action",
                        "action",
                        "order_type",
                        "time_horizon",
                        "verdict",
                    }
                    or kn.endswith("_state")
                    or kn.endswith("_verdict")
                ):
                    # Narrow: match field to enum more carefully below.
                    pass
            coerced = _coerce_by_field(key, node)
            if coerced is not node:
                return coerced
        return node

    cleaned = walk(data, None)
    if isinstance(cleaned, dict):
        return _agent_shape_fixes(cleaned)
    return cleaned


def _coerce_by_field(key: str, value: str) -> Any:
    mapping: dict[str, type[StrEnum]] = {
        "category": NewsCategory,
        "sentiment": Sentiment,
        "market_regime": MarketRegime,
        "overall_verdict": RiskVerdict,
        "verdict": RiskVerdict,
        "portfolio_action": PortfolioAction,
        "action": SymbolAction,
        "order_type": OrderType,
        "time_horizon": TimeHorizon,
        "trend_state": TrendState,
        "market_trend_state": TrendState,
        "momentum_state": MomentumState,
        "market_momentum_state": MomentumState,
        "volatility_state": VolatilityState,
        "market_volatility_state": VolatilityState,
        "breadth_state": BreadthState,
        "market_breadth_state": BreadthState,
        "liquidity_state": LiquidityState,
        "market_liquidity_state": LiquidityState,
    }
    enum_cls = mapping.get(key)
    if enum_cls is None:
        return value
    return coerce_enum_value(enum_cls, value)


def schema_enum_hint() -> str:
    """Compact allowed-values block for system prompts."""
    lines = [
        "ENUMS (use exact strings):",
        f"- sentiment: {', '.join(e.value for e in Sentiment)}",
        f"- category: {', '.join(e.value for e in NewsCategory)}",
        f"- market_regime: {', '.join(e.value for e in MarketRegime)}",
        f"- trend/momentum/volatility/breadth/liquidity *_state: "
        f"{', '.join(e.value for e in TrendState)} / "
        f"{', '.join(e.value for e in MomentumState)} / "
        f"{', '.join(e.value for e in VolatilityState)} / "
        f"{', '.join(e.value for e in BreadthState)} / "
        f"{', '.join(e.value for e in LiquidityState)}",
        f"- overall_verdict: {', '.join(e.value for e in RiskVerdict)}",
        f"- portfolio_action/action: {', '.join(e.value for e in PortfolioAction)}",
        "Booleans must be JSON true/false (not objects).",
        "Scores (data_quality_score, challenge_score, probability*): 0.0-1.0.",
        "cash_target_pct: 0-100. timestamp: ISO-8601 required.",
        "entry_zone must be {min,max}; use upside_scenario/downside_scenario objects "
        "(name/description/probability) — never a bare scenarios key.",
        "macro: expected_sector_impact is [{sector, bias, rationale}, ...] not sector_impacts.",
        "risk output: overall_verdict, cash_pct, gross_exposure_pct — do NOT echo data_quality_score.",
        "devil: prefer_no_trade (bool) + prefer_no_trade_rationale required.",
        "symbol_actions items need: symbol, action, confidence(0-100), target_position_pct, thesis, invalidation.",
    ]
    return "\n".join(lines)
