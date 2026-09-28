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
from app.office.week import reset_office_week_for_tests

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
_CACHE_TTL_SECONDS = 25 * 60
_MIN_ATTEMPT_SECONDS = 8 * 60
_COMMITTEE_GRACE_SECONDS = 45
_HTTP_TIMEOUT_SECONDS = 10.0
_MAX_LINE = 120
_TICKER = re.compile(r"(?<![A-Za-z])[A-Z]{1,5}(?:\.[A-Z]{1,2})?(?![A-Za-z])")
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
    reset_office_week_for_tests()


def remember_office_desk(facts: dict[str, Any] | None) -> None:
    """Keep a tiny book snapshot from /dashboard/summary — no extra queries."""
    global _desk, _desk_fp, _cache_at
    _desk = dict(facts or {})
    fp = json.dumps(_desk, sort_keys=True, default=str)[:2500]
    if fp != _desk_fp:
        _desk_fp = fp
        _cache_at = 0.0


def _session_closed(sess: Any) -> bool:
    if not isinstance(sess, dict) or not sess:
        return False
    if sess.get("is_trading_day") is False:
        return True
    return str(sess.get("phase") or "") == "NON_TRADING_DAY"


def _summary_is_camp(data: dict[str, Any]) -> bool:
    ms = data.get("market_status") or {}
    us = ms.get("us_session") or {}
    venues = ms.get("venue_sessions") if isinstance(ms.get("venue_sessions"), dict) else {}
    au = ms.get("au_session") or venues.get("AU") or {}
    if not au:
        return _session_closed(us)
    return _session_closed(us) and _session_closed(au)


def desk_facts_from_summary(
    snap: dict[str, Any] | None,
    week: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = snap or {}
    port = data.get("portfolio") or {}
    cio = data.get("cio") or {}
    agents = data.get("agents") or {}
    positions = data.get("positions") or []
    universe = data.get("universe") or {}
    losers = [
        p
        for p in positions
        if isinstance(p, dict)
        and _num(p.get("unrealized_pnl")) is not None
        and _num(p.get("unrealized_pnl")) < 0
    ]
    losers.sort(key=lambda p: _num(p.get("unrealized_pnl")) or 0)
    worst = str((losers[0] or {}).get("symbol") or "") if losers else ""
    focus = universe.get("focus") if isinstance(universe, dict) else {}
    focus_n = len((focus or {}).get("symbols") or []) if isinstance(focus, dict) else 0
    clips = _clips_from_summary(data)
    week = week if isinstance(week, dict) else {}
    if not week:
        week = data.get("office_week") if isinstance(data.get("office_week"), dict) else {}
    if week:
        clips["week"] = week
        clips["suggested"] = _uniq_syms(
            list(clips.get("suggested") or []) + list(week.get("suggested") or []),
            limit=6,
        )
        clips["blocked"] = _uniq_syms(
            list(clips.get("blocked") or []) + list(week.get("blocked") or []),
            limit=6,
        )
        clips["missed"] = _uniq_syms(
            list(clips.get("missed") or [])
            + [s for s in (week.get("suggested") or []) if s not in set(week.get("bought") or [])],
            limit=6,
        )
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
            "camp": _summary_is_camp(data),
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


def _human_phrase(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw or "_" in raw or raw.lower() in {"fallback", "fallback_summary", "n/a", "none"}:
        return False
    if re.fullmatch(r"[A-Za-z0-9 .+-]+", raw):
        return False
    return True


def _spoken(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        text = _clean_line(line)
        if text and text not in out and _is_spoken(text):
            out.append(text)
    return out


def spoken_lines_from_facts(facts: dict[str, Any] | None) -> dict[str, list[str]]:
    """Several in-character lines per seat, grounded in the book but said out loud."""
    data = facts or {}
    book = data.get("book") or {}
    agents = data.get("agents") or {}
    action = str(book.get("action") or "")
    regime = str(book.get("regime") or "")
    pnl = _num(book.get("pnl_pct"))
    n_pos = int(book.get("n_pos") or 0)
    worst = str(book.get("worst") or "")
    buying = action in _BUYISH
    sitting = action in _CASHISH or not action
    losing = pnl is not None and pnl < 0
    risk_off = "OFF" in regime.upper()

    cio: list[str] = []
    if losing and buying:
        cio = [
            f"손실 {_pct(pnl)}인데 또 사자고 해서 마음이 불편해.",
            "빨간 날인데 베팅을 키우자고? 난 그게 제일 싫어.",
            f"{worst}부터 아픈데, 여기서 더 사자는 건 성급해."
            if worst
            else "잃는 날에 불 끄는 게 대표 일이야.",
        ]
    elif losing and sitting:
        cio = [
            f"오늘은 {_pct(pnl)}야. 그래서 현금을 쥐고 있는 거야.",
            "잃는 날엔 쉬는 게 내 결재야. 서두르지 마.",
            f"{worst}가 제일 아픈데, 손대면 더 커져."
            if worst
            else "게시판 그대로인 거, 내가 그렇게 둔 거야.",
        ]
    elif sitting:
        cio = [
            "오늘은 현금이 포지션이야. 자리 비운 거 미안해하지 마.",
            "사는 얘기는 내일 장 보고 다시 하자.",
            "급할 거 없어. 결재는 내가 이미 했어.",
            "조용한 날이 나쁜 날은 아니야.",
        ]
    elif buying:
        cio = [
            "조금 더 사자고 한 거야. 확신이 아주 큰 건 아니야.",
            "들어가는 거랑 몰빵은 달라. 크기만 보자.",
            "내가 사인했어. 대신 틀리면 내가 책임져.",
        ]
    else:
        cio = ["장 분위기만 보고 있어. 결론은 아직이야."]

    devil_row = agents.get("devils_advocate") or {}
    challenged = (_num(devil_row.get("challenge")) or 0) >= 0.55
    devil: list[str] = []
    if devil_row.get("prefer_no_trade") and buying:
        devil = [
            "왜 또 사자고 해. 난 오늘 안 사는 쪽이야.",
            "대표가 들어가자고 할 때가 제일 수상해.",
            "조용히 현금 들고 있는 게 맞는데.",
        ]
    elif devil_row.get("prefer_no_trade") or sitting:
        devil = [
            "오늘은 안 사는 게 맞아. 내가 그렇게 봐.",
            "다들 심심하다고 사자고 하지 마.",
            "반대하는 게 내 일이야. 기분 나쁘게 듣지 마.",
            "자신만만한 얼굴이 제일 무서워.",
        ]
    elif challenged:
        why = str(devil_row.get("why") or "")
        devil = [
            _clip(why, 48) if why else "반론이 좀 세. 한 번만 더 생각해.",
            "숫자가 좋아 보여도 스토리가 약해.",
            "내가 꺾으면 그건 칭찬으로 들어.",
        ]
    else:
        devil = [
            "오늘은 굳이 말리진 않을게. 그래도 눈은 안 감아.",
            "동의하는 날도 있어. 드물어서 그래.",
        ]

    risk = agents.get("risk_manager") or {}
    verdict = str(risk.get("verdict") or "")
    if risk.get("halt") or "halt" in verdict.lower():
        risk_lines = [
            "신규 매수는 내가 잠가 뒀어. 버튼 누르지 마.",
            "한도 얘기 또 해야 해? 오늘은 사는 날이 아니야.",
            "규칙은 기분 안 봐. 막힌 건 막힌 거야.",
        ]
    elif risk.get("veto"):
        risk_lines = [
            _clip(f"이건 못 넘겨. {risk.get('veto')}", 48),
            "비토 걸었어. 예외는 대표한테도 없어.",
        ]
    elif "REJECT" in verdict.upper() or verdict == "DENIED":
        risk_lines = [
            "기각이야. 사이즈부터 줄여 와.",
            "통과 못 해. 규칙이 먼저야.",
        ]
    elif buying and losing:
        risk_lines = [
            "승인했어도 손실이 계속이야. 그게 더 걱정돼.",
            "도장 찍었다고 안심해하지 마. 한도는 그대로야.",
        ]
    else:
        risk_lines = [
            "규칙은 지켜졌어. 그래도 방심은 마.",
            "현금이 두둑하면 나도 잔소리가 줄어.",
            "한도 안에서만 놀아. 그 밖은 내 구역이야.",
        ]

    macro = agents.get("macro_strategist") or {}
    mreg = str(macro.get("regime") or regime)
    if "OFF" in mreg.upper() and buying:
        macro_lines = [
            "바람은 위험 회피야. 그런데 왜 사려고 해.",
            "큰 그림이 우산 펴라는데, 소나기 맞으러 나가?",
            "국면이 안 좋은 날의 매수는 내 취향이 아니야.",
        ]
    elif risk_off or "OFF" in mreg.upper():
        macro_lines = [
            "밖은 위험 회피야. 서두를 날 아니야.",
            "파도가 거세. 해변에서 구경만 하자.",
            "매크로가 고개 숙이면 현금이 답이야.",
        ]
    elif "ON" in mreg.upper():
        macro_lines = [
            "바람은 위험 선호 쪽이야. 그래도 뛰진 마.",
            "큰 그림은 괜찮아. 크기만 겸손하게.",
        ]
    elif mreg or regime:
        macro_lines = [
            "바람은 중립이야. 서두를 날이 아니야.",
            "국면이 애매하면 나는 창밖만 봐.",
            "금리랑 환율만 봐도 오늘은 결론이 안 나.",
            "옆으로 누워 있는 장이야. 큰 그림이 그래.",
        ]
    else:
        macro_lines = ["바깥 날씨부터 보자. 아직 말이 안 나와."]

    quant = agents.get("quant_strategist") or {}
    trend = str(quant.get("trend") or "").upper()
    if trend in {"SIDEWAYS", "RANGE", "CHOP"}:
        quant_lines = [
            "차트는 횡보야. 스캘프는 오늘은 쉬자.",
            "봉이 옆으로만 기어가. 존이 안 보여.",
            "숫자 맞춰도 자리가 아니면 난 안 들어가.",
        ]
    elif trend in {"DOWNTREND", "DOWN"}:
        quant_lines = [
            "추세가 아래를 봐. 롱은 짧게만 하자.",
            "떨어지는 봉에 존 그리는 거, 나답지 않아.",
            "차트는 솔직해. 오늘은 롱이 아니야.",
        ]
    elif trend in {"UPTREND", "UP"}:
        quant_lines = [
            "추세는 위야. 그래도 쫓아가면 늦어.",
            "올라가는 봉만 믿지 마. 존이 있어야 들어가.",
        ]
    else:
        quant_lines = [
            "오늘 봉은 심심해. 백테스트 꺼내도 똑같아.",
            "변동은 평범한 편이야. 억지로 만들 자리 없어.",
            "차트가 말을 안 하면 나도 조용히 있어.",
        ]

    mi = agents.get("market_intelligence") or {}
    n_ev = int(mi.get("events") or 0)
    theme = str(mi.get("theme") or "")
    if not _human_phrase(theme):
        theme = ""
    if theme:
        mi_lines = [
            _clip(f"오늘 수다는 {theme} 쪽이야.", 48),
            "기사 제목이랑 실제로 움직이는 종목이 달라.",
            "리서치 노트에 밑줄은 그었어. 매수는 별개야.",
        ]
    elif n_ev == 0:
        mi_lines = [
            "오늘은 헤드라인이 조용해. 심심하네.",
            "뉴스 폴더가 얇아. 억지 테마는 안 만들게.",
            "소문은 많은데 우리 종목이랑은 안 붙어.",
        ]
    else:
        mi_lines = [
            f"뉴스 {n_ev}건 훑었어. 살 얘기는 별로야.",
            "헤드라인은 떠들썩한데 확인이 안 돼.",
            "스크랩만 많고 확신은 없어. 그게 내 오늘이야.",
        ]

    univ = agents.get("universe_manager") or {}
    focus_n = int(univ.get("focus_n") or book.get("focus_n") or 0)
    if n_pos == 0 and sitting:
        univ_lines = [
            "워치에만 이름이 있고 자리는 비었네.",
            "명단은 정리했어. 오늘 살 건 없어.",
            "풀에 있는 거랑 오늘 사는 건 다른 일이야.",
        ]
    elif focus_n:
        univ_lines = [
            f"눈여겨볼 이름 {focus_n}개. 그 밖은 오늘 치워 뒀어.",
            "포커스 먼지 터는 중이야. 이름만 정확하면 돼.",
            "워치에 있어도 자리가 아니면 나는 안 불러.",
        ]
    else:
        univ_lines = [
            "유니버스 다시 펼쳐 봐야겠어. 이름이 흐려.",
            "명단이 비면 나도 할 말이 없어.",
        ]

    raw = {
        "cio": cio,
        "devils_advocate": devil,
        "risk_manager": risk_lines,
        "macro_strategist": macro_lines,
        "quant_strategist": quant_lines,
        "market_intelligence": mi_lines,
        "universe_manager": univ_lines,
    }
    return {k: _spoken(v) for k, v in raw.items() if _spoken(v)}


def role_thoughts_from_facts(facts: dict[str, Any] | None) -> dict[str, str]:
    """One spoken line per seat. Clips about a real ticker replace the generic line."""
    data = facts or {}
    out = {k: v[0] for k, v in spoken_lines_from_facts(data).items() if v}
    out.update(_personality_from_clips(data))
    return {k: v for k, v in out.items() if _clean_line(v) and _is_spoken(v)}


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
    cut = line[:n].rstrip()
    end = max(cut.rfind("."), cut.rfind("?"), cut.rfind("!"))
    if end >= 8:
        return cut[: end + 1]
    return cut


_STATUS_DUMP = re.compile(
    r"\b("
    r"STAY_CASH|SCALE_IN|SCALE_OUT|NO_TRADE|STRONG_BUY|halt_day|halt true|"
    r"NoTrade|Verdict|Regime|conf\s*\d|challenge\s*\d"
    r")\b",
    re.IGNORECASE,
)


def _is_spoken(line: str) -> bool:
    """Floor bubbles are talk, not a field dump from the agent payload."""
    if _STATUS_DUMP.search(line or ""):
        return False
    return len(re.findall(r"[가-힣]", line or "")) >= 4


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
        cut = line[:_MAX_LINE].rstrip()
        end = max(cut.rfind("."), cut.rfind("?"), cut.rfind("!"))
        line = cut[: end + 1] if end >= 8 else cut
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
        "suggested": _uniq_syms(suggested, limit=6),
        "missed": _uniq_syms(missed, limit=6),
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
        out["market_intelligence"] = f"지난주에 {tick} 건의했었는데, 기억나?"
        out["quant_strategist"] = f"{tick} 존 내가 그어 뒀는데 안 들어갔어."
        out["universe_manager"] = f"{tick}는 워치에 있어. 자리는 아직 비었고."
    elif news:
        out["market_intelligence"] = f"{news[0]} 기사 났는데, 우리랑은 조용하네."
    sells = list(clips.get("blocked_sells") or [])
    if blocked and (clips.get("devil_no") or why or clips.get("halt")):
        tick = blocked[0]
        if action in _CASHISH or why or clips.get("halt"):
            out["cio"] = f"{tick}는 내가 승인했는데, 반대해서 결국 못 샀어."
        else:
            out["cio"] = f"{tick} 밀었는데 또 막혔어. 답답하네."
        if clips.get("devil_no"):
            out["devils_advocate"] = f"{tick}는 아니지. 보류가 맞아."
        if clips.get("halt") or clips.get("veto"):
            out["risk_manager"] = f"{tick}는 못 사. 한도가 먼저야."
    elif sells:
        tick = sells[0]
        out["cio"] = f"{tick} 팔고 싶었는데, 반대해서 못 팔았어."
        if clips.get("devil_no"):
            out["devils_advocate"] = f"{tick} 지금 던지지 마. 타이밍이 아니야."
    elif dumped:
        out["cio"] = f"{dumped[0]}는 정리하자고 내가 말했어."
    elif name and action in _CASHISH:
        out["cio"] = f"{name} 사고 싶었는데, 오늘은 현금이 더 편해서 참았어."
    if why and "cio" not in out:
        out["cio"] = _clip(f"막힘: {why}", 52)
    return out


def _regime_said(raw: Any) -> str:
    token = str(raw or "").upper()
    if "OFF" in token:
        return "위험 회피"
    if "ON" in token:
        return "위험 선호"
    if token in {"NEUTRAL", "SIDEWAYS", "RANGE", "CHOP"}:
        return "중립"
    return "애매한"


def _pnl_tok(value: Any) -> str:
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return ""
    return f"+{n}" if n > 0 else str(n)


def _append_line(bucket: dict[str, list[str]], agent: str, line: str) -> None:
    text = _clean_line(line)
    if not text:
        return
    rows = bucket.setdefault(agent, [])
    if text not in rows:
        rows.append(text)


def _review_pool_lines(facts: dict[str, Any]) -> dict[str, list[str]]:
    """Several named one-liners so gossip is not stuck on the first ticker."""
    clips = facts.get("clips") or {}
    book = facts.get("book") or {}
    week = clips.get("week") if isinstance(clips.get("week"), dict) else {}
    out: dict[str, list[str]] = {}
    suggested = list(clips.get("suggested") or [])
    blocked = list(clips.get("blocked") or week.get("blocked") or [])
    for tick in suggested[1:5]:
        _append_line(out, "market_intelligence", f"아, {tick}도 지난주에 건의했었는데.")
        _append_line(out, "quant_strategist", f"{tick} 존도 내가 찍어 뒀었지.")
        _append_line(out, "universe_manager", f"{tick}? 그 이름은 워치에 올려 둔 거야.")
    for tick in blocked[1:4]:
        _append_line(out, "cio", f"{tick}도 밀었는데 결국 막혔어.")
        _append_line(out, "devils_advocate", f"{tick}는 보류가 맞았지. 내가 말렸잖아.")
    loss_cio = (
        "이번 주 {t} {p}. 솔직히 아깝다.",
        "{t}가 이번 주 발목을 잡았어. {p}.",
        "{t} {p}. 그 자리는 오래 붙잡지 말았어야 해.",
    )
    loss_risk = (
        "{t} 손절이 한도 지킨 거야. 욕하지 마.",
        "{t}는 규칙대로 잘랐어. 아깝다고 하지 마.",
        "{t} 손절은 내가 한도 지키려고 한 거야.",
    )
    for i, row in enumerate((week.get("losers") or [])[:3]):
        if not isinstance(row, dict):
            continue
        tick = str(row.get("s") or "")
        pnl = _pnl_tok(row.get("pnl"))
        if not tick:
            continue
        _append_line(out, "cio", loss_cio[i].format(t=tick, p=pnl))
        _append_line(out, "risk_manager", loss_risk[i].format(t=tick))
    win_quant = (
        "{t} 존이 먹혔지. 이번엔 차트가 착했어.",
        "{t}는 내가 그어 둔 자리가 맞았어.",
        "{t} 봉이 존 안에서 버텼어.",
    )
    win_cio = (
        "{t} 이번 주 플러스였어. 이런 날은 조용히 넘기자.",
        "{t}는 잘 나갔어. 더 욕심내진 말자.",
        "{t} 덕에 이번 주가 덜 아프네.",
    )
    for i, row in enumerate((week.get("winners") or [])[:3]):
        if not isinstance(row, dict):
            continue
        tick = str(row.get("s") or "")
        if tick:
            _append_line(out, "quant_strategist", win_quant[i].format(t=tick))
            _append_line(out, "cio", win_cio[i].format(t=tick))
    sold = list(week.get("sold") or [])
    if sold:
        _append_line(out, "cio", f"{sold[0]}는 정리하자고 내가 말했던 거야.")
    if week.get("n_closes"):
        pnl = _pnl_tok(week.get("pnl"))
        if pnl:
            _append_line(out, "cio", f"한 주 손익이 {pnl}이야. 그 정도면 내가 아는 한 주야.")
    regimes = list(week.get("regimes") or [])
    if regimes:
        _append_line(out, "macro_strategist", f"한 주 바람은 {_regime_said(regimes[0])} 쪽이었어.")
    if book.get("camp"):
        _append_line(out, "macro_strategist", "주말이야. 텐트 치고 한 주 바람이나 정리하자.")
        _append_line(out, "universe_manager", "장은 쉬니까 워치 이름만 다시 보자.")
    return out


def thought_pool_from_facts(facts: dict[str, Any] | None) -> dict[str, list[str]]:
    data = facts or {}
    pool: dict[str, list[str]] = {}
    for agent, lines in spoken_lines_from_facts(data).items():
        for line in lines:
            _append_line(pool, agent, line)
    for agent, line in role_thoughts_from_facts(data).items():
        bucket = pool.setdefault(agent, [])
        if line in bucket:
            bucket.remove(line)
        bucket.insert(0, line)
    for agent, lines in _review_pool_lines(data).items():
        for line in lines:
            _append_line(pool, agent, line)
    return {k: v[:6] for k, v in pool.items() if v}


def _thread(
    *,
    topic: str,
    tick: str,
    who: str,
    line: str,
    reply_who: str,
    reply: str,
    close_who: str | None = None,
    close: str | None = None,
) -> dict[str, str]:
    out = {
        "topic": topic,
        "tick": tick,
        "who": who,
        "line": _clean_line(line),
        "reply_who": reply_who,
        "reply": _clean_line(reply),
    }
    if close_who and _clean_line(close or ""):
        out["close_who"] = close_who
        out["close"] = _clean_line(close or "")
    return out


def dialogue_threads(
    facts: dict[str, Any] | None,
    thoughts: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Same-topic 2–3 turn reviews. Tickers only from the book."""
    del thoughts  # threads are built from clips, not mixed pool lines
    data = facts or {}
    clips = data.get("clips") or {}
    week = clips.get("week") if isinstance(clips.get("week"), dict) else {}
    book = data.get("book") or {}
    devil_no = bool(clips.get("devil_no"))
    threads: list[dict[str, str]] = []
    used: set[str] = set()

    def _claim(tick: str) -> bool:
        sym = str(tick or "").upper()
        if not sym or sym in used:
            return False
        used.add(sym)
        return True

    blocked = _uniq_syms(
        list(clips.get("blocked") or []) + list(week.get("blocked") or []), limit=2
    )
    missed = _uniq_syms(
        list(clips.get("missed") or []) + list(week.get("suggested") or []), limit=6
    )
    for tick in blocked:
        if not _claim(tick):
            continue
        closer_who, closer = (
            ("devils_advocate", f"{tick}는 아니지. 보류가 맞아.")
            if devil_no or clips.get("why")
            else ("risk_manager", f"{tick}는 못 사. 한도가 먼저야.")
        )
        threads.append(
            _thread(
                topic="blocked",
                tick=tick,
                who="market_intelligence",
                line=f"야, 지난주에 {tick} 건의했었는데 기억나?",
                reply_who="cio",
                reply=f"{tick}는 내가 승인했는데, 반대해서 결국 못 샀어.",
                close_who=closer_who,
                close=closer,
            )
        )
    for row in (week.get("losers") or [])[:2]:
        if not isinstance(row, dict):
            continue
        tick = str(row.get("s") or "").upper()
        pnl = _pnl_tok(row.get("pnl"))
        if not _claim(tick) or not pnl:
            continue
        threads.append(
            _thread(
                topic="loss",
                tick=tick,
                who="cio",
                line=f"이번 주 {tick} {pnl}. 솔직히 아깝다.",
                reply_who="risk_manager",
                reply=f"{tick} 손절이 한도 지킨 거야. 욕하지 마.",
                close_who="quant_strategist",
                close=f"{tick} 존이 짧았어. 내가 타이트하게 잡았지.",
            )
        )
    for row in (week.get("winners") or [])[:1]:
        if not isinstance(row, dict):
            continue
        tick = str(row.get("s") or "").upper()
        if not _claim(tick):
            continue
        threads.append(
            _thread(
                topic="win",
                tick=tick,
                who="quant_strategist",
                line=f"{tick} 존이 먹혔지. 이번엔 차트가 착했어.",
                reply_who="cio",
                reply=f"{tick} 이번 주 플러스였어. 이런 날은 조용히 넘기자.",
                close_who="universe_manager",
                close=f"{tick}는 워치에 남겨 두자. 이름 빼면 또 찾게 돼.",
            )
        )
    if book.get("camp") or week.get("n_closes"):
        pnl = _pnl_tok(week.get("pnl"))
        reply = (
            f"한 주 손익이 {pnl}이야. 그 정도면 내가 아는 한 주야."
            if pnl
            else "한 주 정리나 해보자. 나는 결론만 들을게."
        )
        wind = _regime_said((week.get("regimes") or ["중립"])[0])
        threads.append(
            _thread(
                topic="week",
                tick="",
                who="macro_strategist",
                line="주말이야. 텐트 치고 한 주 바람이나 정리하자."
                if book.get("camp")
                else f"한 주 바람은 {wind} 쪽이었어. 창밖이 그랬어.",
                reply_who="cio",
                reply=reply,
                close_who="universe_manager",
                close="장은 쉬니까 워치 이름만 다시 보자."
                if book.get("camp")
                else "명단만 다시 맞춰 보자. 자리는 그다음이야.",
            )
        )
    for tick in missed:
        if len(threads) >= 6:
            break
        if not _claim(tick):
            continue
        threads.append(
            _thread(
                topic="suggested",
                tick=tick,
                who="market_intelligence",
                line=f"지난주에 {tick}도 건의했었는데, 뉴스만 보고 끝났어.",
                reply_who="cio",
                reply=f"{tick} 사고 싶었는데, 오늘은 현금이 더 편해서 참았어.",
                close_who="quant_strategist",
                close=f"{tick} 존 내가 그어 뒀는데 안 들어갔어.",
            )
        )
    return [t for t in threads if t.get("line") and t.get("reply")][:6]


def dialogue_beats(
    facts: dict[str, Any] | None,
    thoughts: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Flattened pairs from same-topic threads (compat + one-turn playback)."""
    beats: list[dict[str, str]] = []
    for th in dialogue_threads(facts, thoughts):
        beats.append(
            {
                "who": th["who"],
                "line": th["line"],
                "reply_who": th["reply_who"],
                "reply": th["reply"],
                "topic": th.get("topic") or "",
                "tick": th.get("tick") or "",
            }
        )
        if th.get("close_who") and th.get("close"):
            beats.append(
                {
                    "who": th["reply_who"],
                    "line": th["reply"],
                    "reply_who": th["close_who"],
                    "reply": th["close"],
                    "topic": th.get("topic") or "",
                    "tick": th.get("tick") or "",
                }
            )
    return beats[:8]


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
    pool = thought_pool_from_facts(_desk) if _desk else {}
    for agent, line in cleaned.items():
        bucket = list(pool.get(agent) or [])
        if line and line not in bucket:
            bucket.insert(0, line)
        pool[agent] = bucket[:6]
    pool = {k: v for k, v in pool.items() if v}
    first = {k: v[0] for k, v in pool.items()} if pool else cleaned
    lines = list(first.values()) or list(FALLBACK_LINES[:8])
    threads = dialogue_threads(_desk, first) if _desk else []
    return {
        "enabled": enabled,
        "source": source,
        "reason": reason,
        "thoughts": first,
        "pool": pool,
        "lines": lines,
        "threads": threads,
        "beats": dialogue_beats(_desk, first) if _desk else [],
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
            if not _is_spoken(line):
                continue
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
    camp = bool((_desk.get("book") or {}).get("camp"))
    ttl = 20 * 60 if camp else _CACHE_TTL_SECONDS
    min_attempt = _MIN_ATTEMPT_SECONDS
    if _cache_thoughts and now - _cache_at < ttl:
        return _payload(_merged_thoughts(_cache_thoughts), "cache", None)
    if _generating:
        return _payload(_merged_thoughts(_cache_thoughts), "skipped", "inflight")
    if now - _last_attempt < min_attempt and _cache_thoughts:
        return _payload(_merged_thoughts(_cache_thoughts), "cache", "backoff")
    if _desk and now - _last_attempt >= min_attempt:
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
    week = clips.get("week") if isinstance(clips.get("week"), dict) else {}
    camp = bool(book.get("camp"))
    bits = [
        f"Book pnl={book.get('pnl_pct')} dd={book.get('dd_pct')} cash={book.get('cash_pct')} "
        f"open={book.get('n_pos')} action={book.get('action')} regime={book.get('regime')} "
        f"worst={book.get('worst') or '-'} camp={camp}",
        f"Clips suggested={clips.get('suggested') or []} missed={clips.get('missed') or []} "
        f"blocked={clips.get('blocked') or []} news={clips.get('news') or []} "
        f"why={clips.get('why') or '-'}",
    ]
    if week:
        wins = [
            (w.get("s"), w.get("pnl")) for w in (week.get("winners") or []) if isinstance(w, dict)
        ]
        losses = [
            (w.get("s"), w.get("pnl")) for w in (week.get("losers") or []) if isinstance(w, dict)
        ]
        bits.append(
            f"Week pnl={week.get('pnl')} closes={week.get('n_closes')} "
            f"bought={week.get('bought') or []} sold={week.get('sold') or []} "
            f"blocked={week.get('blocked') or []} suggested={week.get('suggested') or []} "
            f"winners={wins} losers={losses} regimes={week.get('regimes') or []}"
        )
    for name in AGENT_ORDER:
        row = agents.get(name) or {}
        compact = ", ".join(f"{k}={v}" for k, v in row.items() if v not in (None, "", False, []))
        bits.append(f"{name}: {compact or 'n/a'}")
    return "\n".join(bits)[: 1800 if camp or week else 1200]


async def _complete_local_gossip(settings: Settings, facts: dict[str, Any]) -> str:
    api_key = "local"
    if settings.llm_api_key is not None:
        raw_key = settings.llm_api_key.get_secret_value().strip()
        if raw_key:
            api_key = raw_key
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    book = facts.get("book") or {}
    clips = facts.get("clips") or {}
    week = clips.get("week") if isinstance(clips.get("week"), dict) else {}
    camp = bool(book.get("camp"))
    review = camp or bool(week)
    max_tokens = 260 if review else 180
    system = (
        "사무실 잡담. 일곱 명이 서로 다른 말투로 한 줄씩. "
        "cio는 피곤한 대표, risk_manager는 잔소리하는 준법, devils_advocate는 비꼬는 반대, "
        "universe_manager는 명단 정리하는 사람, market_intelligence는 뉴스 수다, "
        "macro_strategist는 날씨처럼 큰 그림, quant_strategist는 차트 덕후. "
        "한국어 반말. 영어 코드, 확신 숫자, 필드 나열 금지. "
        "주어진 종목·손익만 말하고 새 주문은 내지 마. JSON만."
        if review
        else (
            "사무실 잡담. 각자 직무가 묻어나는 한 줄. "
            "cio는 피곤한 대표, risk_manager는 잔소리하는 준법, devils_advocate는 비꼬는 반대, "
            "universe_manager는 명단 정리, market_intelligence는 뉴스 수다, "
            "macro_strategist는 큰 그림, quant_strategist는 차트. "
            "한국어 반말. STAY_CASH, Regime, Verdict, confidence 같은 코드 금지. "
            "없는 종목·숫자는 만들지 마. JSON만."
        )
    )
    payload: dict[str, Any] = {
        "model": _gossip_model(settings),
        "temperature": 0.7,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
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
        "num_ctx": 1024 if not review else 1536,
        "options": {"num_ctx": 1024 if not review else 1536, "num_predict": 220 if review else 160},
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
