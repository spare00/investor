"""Short idle-floor gossip for the Office tab.

Local LLM only, never on the committee path:
- skip when any agent is running (or just finished)
- skip pytest / non-local runtimes
- tiny prompt, hard HTTP timeout, no retries
- GET returns immediately; refill is a background task
- cache lines for minutes so the GPU is barely touched
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

from app.agents.activity import snapshot_agent_activity
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
    "회의는 아닌 듯.",
    "프린터가 또야.",
    "오늘 바닥 미끄럽네.",
    "커피머신 줄 섰어.",
)

_CACHE_TTL_SECONDS = 15 * 60
_MIN_ATTEMPT_SECONDS = 2 * 60
_COMMITTEE_GRACE_SECONDS = 45
_HTTP_TIMEOUT_SECONDS = 8.0
_MAX_LINE = 32
_JSON_BLOB = re.compile(r"\{.*\}", re.DOTALL)

_cache_lines: list[str] = []
_cache_at = 0.0
_last_attempt = 0.0
_generating = False


def reset_office_gossip_for_tests() -> None:
    global _cache_lines, _cache_at, _last_attempt, _generating
    _cache_lines = []
    _cache_at = 0.0
    _last_attempt = 0.0
    _generating = False


def parse_gossip_lines(raw: str, *, limit: int = 10) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return []
    blob = text
    match = _JSON_BLOB.search(text)
    if match:
        blob = match.group(0)
    items: list[Any] = []
    try:
        parsed = json.loads(blob)
        if isinstance(parsed, dict):
            items = parsed.get("lines") or parsed.get("gossip") or []
        elif isinstance(parsed, list):
            items = parsed
    except json.JSONDecodeError:
        items = []
    if not items:
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
    if any(tok in lowered for tok in ("http://", "https://", "buy ", "sell ", "ticker")):
        return ""
    if len(line) > _MAX_LINE:
        line = line[: _MAX_LINE - 1].rstrip() + "…"
    return line


def _payload(
    lines: list[str] | tuple[str, ...],
    source: str,
    reason: str | None,
    *,
    enabled: bool = True,
) -> dict[str, Any]:
    cleaned = [ln for ln in (_clean_line(x) for x in lines) if ln][:8]
    if not cleaned:
        cleaned = list(FALLBACK_LINES[:8])
    return {
        "enabled": enabled,
        "source": source,
        "reason": reason,
        "lines": cleaned,
    }


def _in_automated_test(settings: Settings) -> bool:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    env = settings.app_env
    name = env.value if hasattr(env, "value") else str(env)
    return name.lower() in {"test", "testing"}


async def next_office_gossip(settings: Settings | None = None) -> dict[str, Any]:
    """Return a handful of short lines. Never blocks on the local model."""
    cfg = settings or get_settings()
    if committee_holding_gpu():
        return _payload(FALLBACK_LINES, "skipped", "committee_busy")
    if _in_automated_test(cfg):
        return _payload(FALLBACK_LINES, "fallback", "test")
    if not cfg.llm_is_local():
        return _payload(FALLBACK_LINES, "fallback", "not_local")

    now = time.monotonic()
    if _cache_lines and now - _cache_at < _CACHE_TTL_SECONDS:
        return _payload(_cache_lines, "cache", None)
    if _generating:
        return _payload(_cache_lines or FALLBACK_LINES, "skipped", "inflight")
    if now - _last_attempt < _MIN_ATTEMPT_SECONDS and _cache_lines:
        return _payload(_cache_lines, "cache", "backoff")
    if now - _last_attempt >= _MIN_ATTEMPT_SECONDS:
        _schedule_fill(cfg)
    return _payload(_cache_lines or FALLBACK_LINES, "fallback" if not _cache_lines else "cache", "pending")


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
    global _cache_lines, _cache_at, _generating
    try:
        if committee_holding_gpu():
            logger.info("office_gossip_abort", reason="committee_busy")
            return
        raw = await _complete_local_gossip(settings)
        lines = parse_gossip_lines(raw)
        if lines:
            _cache_lines = lines
            _cache_at = time.monotonic()
            logger.info("office_gossip_filled", n=len(lines), model=_gossip_model(settings))
        else:
            logger.info("office_gossip_empty")
    except Exception as exc:  # noqa: BLE001 — floor toy must not raise into the loop
        logger.warning("office_gossip_failed", error=str(exc)[:180])
    finally:
        _generating = False


def _gossip_model(settings: Settings) -> str:
    fast = (settings.llm_local_fast_model or "").strip()
    return fast or settings.llm_model


async def _complete_local_gossip(settings: Settings) -> str:
    api_key = "local"
    if settings.llm_api_key is not None:
        raw_key = settings.llm_api_key.get_secret_value().strip()
        if raw_key:
            api_key = raw_key
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": _gossip_model(settings),
        "temperature": 0.9,
        "max_tokens": 80,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Write tiny water-cooler gossip for a six-person trading office. "
                    "Casual Korean 반말. No advice, no tickers, no secrets. "
                    "Each line under 18 Korean characters."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Staff nicknames: 대표 CIO, 준법 Risk, 반대 Devil, 유니버스 Univ, "
                    "리서치 MI, 매크로 Macro, 퀀트 Quant. "
                    'JSON only: {"lines":["...","..."]} with 8 lines about coffee, '
                    "the board, weather, mugs, or the quiet floor."
                ),
            },
        ],
        "num_ctx": 512,
        "options": {"num_ctx": 512, "num_predict": 64},
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
