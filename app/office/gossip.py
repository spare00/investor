"""Short idle-floor talk for the Office tab.

Local LLM only, never on the committee path:
- skip when any agent is running (or just finished)
- skip pytest / non-local runtimes
- tiny prompt, hard HTTP timeout, no retries
- GET returns immediately; refill is a background task
- cache lines for minutes so the GPU is barely touched

Idle speech is role-thoughts grounded in the last dashboard book
(CIO worries about losses, Devil objects to buying a bad tape, …).
Coffee-chat is only the last resort when no desk facts exist.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from app.agents.activity import AGENT_ORDER, AGENT_SHORT, snapshot_agent_activity
from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

FALLBACK_LINES: tuple[str, ...] = (
    "커피 또 내렸어.",
    "게시판 안 바뀌네.",
    "누가 머그 놓고 감.",
    "창가 좀 춥다.",
    "점심 뭐 먹지.",
    "화분에 물 줘야지.",
    "복도 조용하다.",
    "모니터 깜빡이네.",
)

_BUYISH = frozenset({"BUY", "STRONG_BUY", "SCALE_IN"})
_CASHISH = frozenset({"NO_TRADE", "HOLD", "STAY_CASH", "CASH", "REDUCE", "SCALE_OUT"})
_CACHE_TTL_SECONDS = 12 * 60
_MIN_ATTEMPT_SECONDS = 2 * 60
_COMMITTEE_GRACE_SECONDS = 45
_HTTP_TIMEOUT_SECONDS = 10.0
_MAX_LINE = 56
_TICKER = re.compile(r"\b[A-Z]{1,5}(?:\.[A-Z]{1,2})?\b")
_JSON_BLOB = re.compile(r"\{.*\}", re.DOTALL)

_KEY_ALIASES = {
    **{name: name for name in AGENT_ORDER},
    **{short.lower(): name for name, short in AGENT_SHORT.items()},
    "devil": "devils_advocate",
    "risk": "risk_manager",
    "macro": "macro_strategist",
    "quant": "quant_strategist",
    "mi": "market_intelligence",
    "univ": "universe_manager",
    "대표": "cio",
    "반대": "devils_advocate",
    "준법": "risk_manager",
}

_cache_thoughts: dict[str, str] = {}
_cache_at = 0.0
_last_attempt = 0.0
_generating = False
_desk: dict[str, Any] = {}
_desk_fp = ""


def reset_office_gossip_for_tests() -> None:
    global _cache_thoughts, _cache_at, _last_attempt, _generating, _desk, _desk_fp
    _cache_thoughts = {}
    _cache_at = 0.0
    _last_attempt = 0.0
    _generating = False
    _desk = {}
    _desk_fp = ""


def remember_office_desk(facts: dict[str, Any] | None) -> None:
    """Keep a tiny book snapshot from /dashboard/summary — no extra queries."""
    global _desk, _desk_fp, _cache_at
    _desk = dict(facts or {})
    fp = json.dumps(_desk, sort_keys=True, default=str)[:2500]
    if fp != _desk_fp:
        _desk_fp = fp
        _cache_at = 0.0


def desk_facts_from_summary(snap: dict[str, Any] | None) -> dict[str, Any]:
    data = snap or {}
    port = data.get("portfolio") or {}
    cio = data.get("cio") or {}
    agents = data.get("agents") or {}
    positions = data.get("positions") or []
    universe = data.get("universe") or {}
    losers = [
        p
        for p in positions
        if isinstance(p, dict) and _num(p.get("unrealized_pnl")) is not None and _num(p.get("unrealized_pnl")) < 0
    ]
    losers.sort(key=lambda p: _num(p.get("unrealized_pnl")) or 0)
    worst = str((losers[0] or {}).get("symbol") or "") if losers else ""
    focus = universe.get("focus") if isinstance(universe, dict) else {}
    focus_n = len((focus or {}).get("symbols") or []) if isinstance(focus, dict) else 0
    clips = _clips_from_summary(data)
    return {
        "book": {
            "pnl_pct": _num(port.get("daily_pnl_pct")),
            "dd_pct": _num(port.get("drawdown_pct")),
            "cash_pct": _num(port.get("cash_pct")),
            "n_pos": len(positions) or int(port.get("open_positions") or 0),
            "action": str(cio.get("portfolio_action") or ""),
            "regime": str(cio.get("market_regime") or ""),
            "worst": worst,
            "focus_n": focus_n,
        },
        "clips": clips,
        "agents": {
            "cio": _cio_slice(cio, agents.get("cio") or {}),
            "devils_advocate": _devil_slice(agents.get("devils_advocate") or {}),
            "risk_manager": _risk_slice(agents.get("risk_manager") or {}),
            "macro_strategist": _macro_slice(agents.get("macro_strategist") or {}),
            "quant_strategist": _quant_slice(agents.get("quant_strategist") or {}),
            "market_intelligence": _mi_slice(agents.get("market_intelligence") or {}),
            "universe_manager": _univ_slice(agents.get("universe_manager") or {}, focus_n),
        },
    }


def role_thoughts_from_facts(facts: dict[str, Any] | None) -> dict[str, str]:
    """Deterministic one-liners from the live book — useful even when the LLM is skipped."""
    data = facts or {}
    book = data.get("book") or {}
    agents = data.get("agents") or {}
    action = str(book.get("action") or "")
    regime = str(book.get("regime") or "")
    pnl = _num(book.get("pnl_pct"))
    cash = _num(book.get("cash_pct"))
    n_pos = int(book.get("n_pos") or 0)
    worst = str(book.get("worst") or "")
    out: dict[str, str] = {}

    cio = agents.get("cio") or {}
    if pnl is not None and pnl < 0 and action in _BUYISH:
        out["cio"] = f"손실 {_pct(pnl)}인데 {action}이라 걱정돼."
    elif pnl is not None and pnl < 0 and action in _CASHISH:
        out["cio"] = f"손실 {_pct(pnl)}. {action} 유지."
    elif pnl is not None and pnl < 0:
        tail = f" {worst}부터." if worst else ""
        out["cio"] = f"오늘 {_pct(pnl)}.{tail}"
    elif action in _CASHISH:
        cash_s = f" 현금 {_pct(cash, False)}." if cash is not None else ""
        out["cio"] = f"{action} 유지.{cash_s}"
    elif action:
        out["cio"] = f"{action} · {regime or cio.get('regime') or '장'} 보고 있어."

    devil = agents.get("devils_advocate") or {}
    rec = str(devil.get("recommendation") or "")
    if devil.get("prefer_no_trade") and action in _BUYISH:
        out["devils_advocate"] = f"{regime or '이 장'}인데 왜 {action}이야."
    elif rec in {"NO_TRADE", "WAIT"} and action in _BUYISH:
        out["devils_advocate"] = f"난 {rec}인데 왜 또 사자고 해."
    elif _num(devil.get("challenge")) is not None and (_num(devil.get("challenge")) or 0) >= 0.55:
        out["devils_advocate"] = _clip(devil.get("why") or f"반론 {(_num(devil.get('challenge')) or 0):.2f}.", 48)
    elif devil.get("prefer_no_trade"):
        out["devils_advocate"] = "오늘은 안 사는 게 맞아."

    risk = agents.get("risk_manager") or {}
    verdict = str(risk.get("verdict") or "")
    if risk.get("halt") or "halt" in verdict.lower():
        out["risk_manager"] = "신규 매수 멈춰야 해."
    elif risk.get("veto"):
        out["risk_manager"] = _clip(f"비토: {risk.get('veto')}", 48)
    elif "REJECT" in verdict.upper() or verdict == "DENIED":
        out["risk_manager"] = f"판결 {verdict}. 사이즈 줄여."
    elif action in _BUYISH and pnl is not None and pnl < 0:
        out["risk_manager"] = "승인했어도 손실이 계속이야."
    elif verdict:
        out["risk_manager"] = f"판결 {verdict} · 현금 {_pct(cash, False) if cash is not None else '?'}."

    macro = agents.get("macro_strategist") or {}
    mreg = str(macro.get("regime") or regime)
    if "OFF" in mreg.upper() and action in _BUYISH:
        out["macro_strategist"] = f"{mreg}인데 왜 사나."
    elif mreg:
        conf = _num(macro.get("confidence"))
        conf_s = f" · 확신 {conf:.2f}" if conf is not None else ""
        out["macro_strategist"] = f"레짐 {mreg}{conf_s}."

    quant = agents.get("quant_strategist") or {}
    trend = str(quant.get("trend") or "")
    if trend.upper() in {"SIDEWAYS", "RANGE", "CHOP"}:
        out["quant_strategist"] = "횡보인데 스캘프 그만하자."
    elif trend.upper() in {"DOWNTREND", "DOWN"}:
        out["quant_strategist"] = "추세가 아래야. 롱 조심."
    elif trend:
        out["quant_strategist"] = f"추세 {trend} · 변동 {quant.get('vol') or '?'}."

    mi = agents.get("market_intelligence") or {}
    n_ev = int(mi.get("events") or 0)
    theme = str(mi.get("theme") or "")
    if n_ev == 0:
        out["market_intelligence"] = "오늘은 뉴스 비었어."
    elif theme:
        out["market_intelligence"] = _clip(f"테마 {theme}", 48)
    else:
        out["market_intelligence"] = f"이벤트 {n_ev}건 · q {mi.get('quality') or '?'}."

    univ = agents.get("universe_manager") or {}
    focus_n = int(univ.get("focus_n") or book.get("focus_n") or 0)
    if n_pos == 0 and action in _CASHISH:
        out["universe_manager"] = "워치만 있고 자리는 비었어."
    elif focus_n:
        out["universe_manager"] = f"포커스 {focus_n}개. 이름은 맞나."
    else:
        out["universe_manager"] = "유니버스 다시 봐야 해."

    out.update(_personality_from_clips(data))
    return {k: v for k, v in out.items() if _clean_line(v)}


def parse_gossip_lines(raw: str, *, limit: int = 10) -> list[str]:
    thoughts = parse_gossip_thoughts(raw)
    if thoughts:
        return list(thoughts.values())[:limit]
    text = (raw or "").strip()
    if not text:
        return []
    items: list[Any] = []
    match = _JSON_BLOB.search(text)
    blob = match.group(0) if match else text
    try:
        parsed = json.loads(blob)
        if isinstance(parsed, dict):
            items = parsed.get("lines") or parsed.get("gossip") or []
        elif isinstance(parsed, list):
            items = parsed
    except json.JSONDecodeError:
        items = [ln for ln in text.splitlines() if ln.strip()]
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        line = _clean_line(item)
        if not line or line in seen:
            continue
        seen.add(line)
        out.append(line)
        if len(out) >= limit:
            break
    return out


def parse_gossip_thoughts(raw: str) -> dict[str, str]:
    text = (raw or "").strip()
    if not text:
        return {}
    match = _JSON_BLOB.search(text)
    blob = match.group(0) if match else text
    try:
        parsed = json.loads(blob)
    except json.JSONDecodeError:
        return _thoughts_from_labeled_lines(text)
    if not isinstance(parsed, dict):
        return {}
    block = parsed.get("thoughts") or parsed.get("speech") or parsed
    out: dict[str, str] = {}
    if isinstance(block, dict):
        for key, value in block.items():
            agent = _agent_key(key)
            line = _clean_line(value)
            if agent and line:
                out[agent] = line
    elif isinstance(block, list):
        for item in block:
            if not isinstance(item, dict):
                continue
            agent = _agent_key(item.get("id") or item.get("who") or item.get("agent"))
            line = _clean_line(item.get("text") or item.get("line"))
            if agent and line:
                out[agent] = line
    return out


def committee_holding_gpu(now: datetime | None = None) -> bool:
    """True when a committee chat is live or just finished — do not steal the GPU."""
    now = now or datetime.now(UTC)
    live = snapshot_agent_activity()
    for row in live.values():
        if not isinstance(row, dict):
            continue
        if str(row.get("state") or "") == "running":
            return True
        for key in ("finished_at", "started_at"):
            stamp = _as_dt(row.get(key))
            if stamp is None:
                continue
            age = (now - stamp).total_seconds()
            if 0 <= age < _COMMITTEE_GRACE_SECONDS:
                return True
    return False


def _as_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        ts = value
    else:
        try:
            ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def _num(value: Any) -> float | None:
    if value is None or value is False:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct(value: float, signed: bool = True) -> str:
    if signed:
        return f"{value:+.1f}%"
    return f"{value:.0f}%"


def _clip(value: Any, n: int) -> str:
    line = " ".join(str(value or "").split())
    if len(line) <= n:
        return line
    return line[: n - 1].rstrip() + "…"


def _clean_line(item: Any) -> str:
    if not isinstance(item, str):
        return ""
    line = item.strip().strip("`").strip()
    line = re.sub(r"^[\-\*\d\.\)\]]+\s*", "", line)
    line = " ".join(line.split())
    if len(line) < 4:
        return ""
    if line.startswith("{") or line.startswith("["):
        return ""
    lowered = line.lower()
    if "http://" in lowered or "https://" in lowered:
        return ""
    if len(line) > _MAX_LINE:
        line = line[: _MAX_LINE - 1].rstrip() + "…"
    return line


def _agent_key(raw: Any) -> str | None:
    token = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    return _KEY_ALIASES.get(token)


def _thoughts_from_labeled_lines(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        if ":" not in raw:
            continue
        who, _, rest = raw.partition(":")
        agent = _agent_key(who)
        line = _clean_line(rest)
        if agent and line:
            out[agent] = line
    return out


def _cio_slice(cio: dict[str, Any], agent: dict[str, Any]) -> dict[str, Any]:
    payload = agent.get("payload") if isinstance(agent.get("payload"), dict) else {}
    return {
        "action": cio.get("portfolio_action") or payload.get("portfolio_action"),
        "regime": cio.get("market_regime") or payload.get("market_regime"),
        "risk": cio.get("risk_approval"),
    }


def _devil_slice(agent: dict[str, Any]) -> dict[str, Any]:
    p = agent.get("payload") if isinstance(agent.get("payload"), dict) else {}
    why = p.get("prefer_no_trade_rationale") or p.get("strongest_reason_thesis_is_wrong") or ""
    return {
        "prefer_no_trade": bool(p.get("prefer_no_trade")),
        "challenge": _num(p.get("challenge_score")),
        "recommendation": p.get("recommendation"),
        "why": _clip(why, 72),
    }


def _risk_slice(agent: dict[str, Any]) -> dict[str, Any]:
    p = agent.get("payload") if isinstance(agent.get("payload"), dict) else {}
    vetoes = p.get("hard_vetoes") or []
    veto = vetoes[0] if isinstance(vetoes, list) and vetoes else ""
    return {
        "verdict": p.get("overall_verdict"),
        "halt": bool(p.get("halt_new_trades")),
        "veto": _clip(veto, 48) if veto else "",
        "cash": _num(p.get("cash_pct")),
    }


def _macro_slice(agent: dict[str, Any]) -> dict[str, Any]:
    p = agent.get("payload") if isinstance(agent.get("payload"), dict) else {}
    return {"regime": p.get("market_regime"), "confidence": _num(p.get("confidence"))}


def _quant_slice(agent: dict[str, Any]) -> dict[str, Any]:
    p = agent.get("payload") if isinstance(agent.get("payload"), dict) else {}
    return {
        "trend": p.get("market_trend_state"),
        "vol": p.get("market_volatility_state"),
    }


def _mi_slice(agent: dict[str, Any]) -> dict[str, Any]:
    p = agent.get("payload") if isinstance(agent.get("payload"), dict) else {}
    events = p.get("market_events") or []
    themes = p.get("top_market_themes") or []
    theme = themes[0] if isinstance(themes, list) and themes else ""
    return {
        "events": len(events) if isinstance(events, list) else 0,
        "theme": _clip(theme, 40),
        "quality": _num(p.get("data_quality_score")),
    }


def _univ_slice(agent: dict[str, Any], focus_n: int) -> dict[str, Any]:
    p = agent.get("payload") if isinstance(agent.get("payload"), dict) else {}
    n = focus_n or len(p.get("focus_symbols") or p.get("watchlist") or p.get("symbols") or [])
    return {"focus_n": n, "note": _clip(p.get("focus_rationale") or p.get("mode") or "", 48)}


def _uniq_syms(values: list[str], *, limit: int = 3) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        sym = str(raw or "").upper().strip()
        if not sym or sym in {"—", "-", "N/A"} or len(sym) > 6:
            continue
        if not re.fullmatch(r"[A-Z]{1,5}(\.[A-Z]{1,2})?", sym):
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
    return str(getattr(raw, "value", raw) or "")


def _clips_from_summary(data: dict[str, Any]) -> dict[str, Any]:
    """Tiny named-ticker memory from the last summary — no extra queries."""
    cio = data.get("cio") or {}
    payload = cio.get("payload") if isinstance(cio.get("payload"), dict) else {}
    plans = payload.get("symbol_actions") or []
    approved: list[str] = []
    dumped: list[str] = []
    if isinstance(plans, list):
        for plan in plans:
            if not isinstance(plan, dict):
                continue
            sym = str(plan.get("symbol") or "").upper()
            act = _plan_action(plan)
            if act in _BUYISH:
                approved.append(sym)
            elif act in {"SELL", "PARTIAL_SELL", "REDUCE"}:
                dumped.append(sym)

    agents = data.get("agents") or {}
    quant = agents.get("quant_strategist") or {}
    qpay = quant.get("payload") if isinstance(quant.get("payload"), dict) else {}
    suggested: list[str] = []
    for view in qpay.get("symbol_views") or []:
        if not isinstance(view, dict):
            continue
        if view.get("entry_zone") is None:
            continue
        suggested.append(str(view.get("symbol") or ""))

    news_syms: list[str] = []
    for item in data.get("news") or []:
        if not isinstance(item, dict):
            continue
        for sym in item.get("symbols") or []:
            news_syms.append(str(sym or ""))

    devil = agents.get("devils_advocate") or {}
    dpay = devil.get("payload") if isinstance(devil.get("payload"), dict) else {}
    devil_no = bool(dpay.get("prefer_no_trade"))
    why = str(payload.get("reason_not_to_trade") or cio.get("reason_not_to_trade") or "")
    risk = agents.get("risk_manager") or {}
    rpay = risk.get("payload") if isinstance(risk.get("payload"), dict) else {}
    vetoes = rpay.get("hard_vetoes") or []
    veto = str(vetoes[0]) if isinstance(vetoes, list) and vetoes else ""
    halted = devil_no or bool(why) or bool(rpay.get("halt_new_trades"))
    blocked = (list(approved) or list(suggested)) if halted else []
    blocked_sells = list(dumped) if halted else []
    missed = [s for s in suggested if s.upper() not in {x.upper() for x in approved}]
    return {
        "approved": _uniq_syms(approved),
        "dumped": _uniq_syms(dumped),
        "suggested": _uniq_syms(suggested),
        "missed": _uniq_syms(missed),
        "news": _uniq_syms(news_syms),
        "blocked": _uniq_syms(blocked),
        "blocked_sells": _uniq_syms(blocked_sells),
        "why": _clip(why, 40),
        "devil_no": devil_no,
        "veto": _clip(veto, 40),
        "halt": bool(rpay.get("halt_new_trades")),
    }


def _personality_from_clips(facts: dict[str, Any]) -> dict[str, str]:
    clips = facts.get("clips") or {}
    book = facts.get("book") or {}
    action = str(book.get("action") or "")
    suggested = list(clips.get("suggested") or [])
    missed = list(clips.get("missed") or suggested)
    blocked = list(clips.get("blocked") or [])
    dumped = list(clips.get("dumped") or [])
    news = list(clips.get("news") or [])
    why = str(clips.get("why") or "")
    name = (blocked or missed or suggested or dumped or news or [""])[0]
    out: dict[str, str] = {}
    if missed or suggested:
        tick = (missed or suggested)[0]
        out["market_intelligence"] = f"지난번에 {tick} 건의했는데."
        out["quant_strategist"] = f"{tick} 존 냈는데 안 들어갔어."
        out["universe_manager"] = f"{tick} 워치에 있는데 자리 없네."
    elif news:
        out["market_intelligence"] = f"{news[0]} 뉴스 떴는데 조용하네."
    sells = list(clips.get("blocked_sells") or [])
    if blocked and (clips.get("devil_no") or why or clips.get("halt")):
        tick = blocked[0]
        if action in _CASHISH or why or clips.get("halt"):
            out["cio"] = f"{tick} 승인했는데 반대해서 못 샀어."
        else:
            out["cio"] = f"{tick} 밀었는데 또 막혔어."
        if clips.get("devil_no"):
            out["devils_advocate"] = f"{tick}는 아니지. 보류가 맞아."
        if clips.get("halt") or clips.get("veto"):
            out["risk_manager"] = f"{tick} 못 사. 한도야."
    elif sells:
        tick = sells[0]
        out["cio"] = f"{tick} 팔려 했는데 반대해서 못 팔았어."
        if clips.get("devil_no"):
            out["devils_advocate"] = f"{tick} 지금 던지지 마."
    elif dumped:
        out["cio"] = f"{dumped[0]} 정리하라고 했어."
    elif name and action in _CASHISH:
        out["cio"] = f"{name} 사고 싶었는데 {action}."
    if why and "cio" not in out:
        out["cio"] = _clip(f"막힘: {why}", 52)
    return out


def dialogue_beats(
    facts: dict[str, Any] | None,
    thoughts: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """One-turn reply pairs grounded in clips. No extra model call."""
    thoughts = thoughts or role_thoughts_from_facts(facts)
    clips = (facts or {}).get("clips") or {}
    beats: list[dict[str, str]] = []

    def _beat(who: str, reply_who: str) -> None:
        a = thoughts.get(who)
        b = thoughts.get(reply_who)
        if not a or not b:
            return
        beats.append({"who": who, "line": a, "reply_who": reply_who, "reply": b})

    if clips.get("blocked") or clips.get("devil_no"):
        _beat("cio", "devils_advocate")
        _beat("cio", "risk_manager")
    if clips.get("missed") or clips.get("suggested"):
        _beat("market_intelligence", "cio")
        _beat("quant_strategist", "cio")
    if not beats:
        _beat("macro_strategist", "quant_strategist")
        _beat("cio", "devils_advocate")
    return beats[:4]


def _has_ticker(line: str) -> bool:
    return bool(_TICKER.search(line or ""))


def _payload(
    thoughts: dict[str, str],
    source: str,
    reason: str | None,
    *,
    enabled: bool = True,
) -> dict[str, Any]:
    cleaned = {k: _clean_line(v) for k, v in thoughts.items() if _agent_key(k) and _clean_line(v)}
    cleaned = {_agent_key(k) or k: v for k, v in cleaned.items()}
    lines = list(cleaned.values()) or list(FALLBACK_LINES[:8])
    if not cleaned:
        cleaned = {}
    return {
        "enabled": enabled,
        "source": source,
        "reason": reason,
        "thoughts": cleaned,
        "lines": lines,
        "beats": dialogue_beats(_desk, cleaned) if _desk else [],
    }


def _merged_thoughts(llm: dict[str, str] | None = None) -> dict[str, str]:
    base = role_thoughts_from_facts(_desk) if _desk else {}
    if llm:
        for key, value in llm.items():
            agent = _agent_key(key)
            line = _clean_line(value)
            if not agent or not line:
                continue
            prev = base.get(agent)
            if prev and _has_ticker(prev) and not _has_ticker(line):
                continue
            base[agent] = line
    return base


def _in_automated_test(settings: Settings) -> bool:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    env = settings.app_env
    name = env.value if hasattr(env, "value") else str(env)
    return name.lower() in {"test", "testing"}


async def next_office_gossip(settings: Settings | None = None) -> dict[str, Any]:
    """Return per-staff thoughts. Never blocks on the local model."""
    cfg = settings or get_settings()
    if committee_holding_gpu():
        return _payload(_merged_thoughts(), "skipped", "committee_busy")
    if _in_automated_test(cfg):
        return _payload(_merged_thoughts(), "fallback", "test")
    if not cfg.llm_is_local():
        return _payload(_merged_thoughts(), "fallback", "not_local")

    now = time.monotonic()
    if _cache_thoughts and now - _cache_at < _CACHE_TTL_SECONDS:
        return _payload(_merged_thoughts(_cache_thoughts), "cache", None)
    if _generating:
        return _payload(_merged_thoughts(_cache_thoughts), "skipped", "inflight")
    if now - _last_attempt < _MIN_ATTEMPT_SECONDS and _cache_thoughts:
        return _payload(_merged_thoughts(_cache_thoughts), "cache", "backoff")
    if _desk and now - _last_attempt >= _MIN_ATTEMPT_SECONDS:
        _schedule_fill(cfg)
    return _payload(
        _merged_thoughts(_cache_thoughts),
        "fallback" if not _cache_thoughts else "cache",
        "pending" if _desk else "no_desk",
    )


def _schedule_fill(settings: Settings) -> None:
    global _generating, _last_attempt
    _generating = True
    _last_attempt = time.monotonic()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _generating = False
        return
    loop.create_task(_fill_cache(settings), name="office-gossip-fill")


async def _fill_cache(settings: Settings) -> None:
    global _cache_thoughts, _cache_at, _generating
    try:
        if committee_holding_gpu() or not _desk:
            logger.info("office_gossip_abort", reason="committee_or_no_desk")
            return
        raw = await _complete_local_gossip(settings, _desk)
        thoughts = parse_gossip_thoughts(raw)
        if thoughts:
            _cache_thoughts = thoughts
            _cache_at = time.monotonic()
            logger.info("office_gossip_filled", n=len(thoughts), model=_gossip_model(settings))
        else:
            logger.info("office_gossip_empty")
    except Exception as exc:  # noqa: BLE001 — floor toy must not raise into the loop
        logger.warning("office_gossip_failed", error=str(exc)[:180])
    finally:
        _generating = False


def _gossip_model(settings: Settings) -> str:
    fast = (settings.llm_local_fast_model or "").strip()
    return fast or settings.llm_model


def _desk_prompt(facts: dict[str, Any]) -> str:
    book = facts.get("book") or {}
    agents = facts.get("agents") or {}
    clips = facts.get("clips") or {}
    bits = [
        f"Book pnl={book.get('pnl_pct')} dd={book.get('dd_pct')} cash={book.get('cash_pct')} "
        f"open={book.get('n_pos')} action={book.get('action')} regime={book.get('regime')} "
        f"worst={book.get('worst') or '-'}",
        f"Clips suggested={clips.get('suggested') or []} missed={clips.get('missed') or []} "
        f"blocked={clips.get('blocked') or []} news={clips.get('news') or []} "
        f"why={clips.get('why') or '-'}",
    ]
    for name in AGENT_ORDER:
        row = agents.get(name) or {}
        compact = ", ".join(f"{k}={v}" for k, v in row.items() if v not in (None, "", False, []))
        bits.append(f"{name}: {compact or 'n/a'}")
    return "\n".join(bits)[:1200]


async def _complete_local_gossip(settings: Settings, facts: dict[str, Any]) -> str:
    api_key = "local"
    if settings.llm_api_key is not None:
        raw_key = settings.llm_api_key.get_secret_value().strip()
        if raw_key:
            api_key = raw_key
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": _gossip_model(settings),
        "temperature": 0.7,
        "max_tokens": 180,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Each staff member says one short line in THEIR job voice from the facts. "
                    "Casual Korean 반말. Use named tickers when given. Do not invent names or numbers. "
                    "Do not issue new orders. Max 32 Korean characters. JSON only."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Keys: cio, risk_manager, devils_advocate, universe_manager, "
                    "market_intelligence, macro_strategist, quant_strategist.\n"
                    + _desk_prompt(facts)
                    + '\nJSON: {"thoughts":{"cio":"...","devils_advocate":"..."}}'
                ),
            },
        ],
        "num_ctx": 1024,
        "options": {"num_ctx": 1024, "num_predict": 160},
    }
    timeout = httpx.Timeout(_HTTP_TIMEOUT_SECONDS, connect=2.0)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, headers=headers, json=payload)
        if response.status_code >= 400:
            raise RuntimeError(f"LLM HTTP {response.status_code}")
        data = response.json()
    try:
        return str(data["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Unexpected LLM response shape") from exc
