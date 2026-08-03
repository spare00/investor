"""Daily LLM spend budget — hard-stop unexpected token/call blowups.

Process-local counters keyed by UTC date. Optional JSON state file survives restarts
within the same calendar day. When exceeded, real API calls raise LLMBudgetExceeded
so agents can fall back / skip instead of burning more tokens.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_lock = threading.Lock()
_state_day: str | None = None
_prompt_tokens = 0
_completion_tokens = 0
_calls = 0
_warned_soft = False


class LLMBudgetExceeded(Exception):
    """Raised when the daily LLM budget would be exceeded by another call."""

    def __init__(self, reason: str, snapshot: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.snapshot = snapshot or {}


@dataclass(frozen=True, slots=True)
class LLMBudgetSnapshot:
    day: str
    enforce: bool
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    calls: int
    token_budget: int
    call_budget: int
    soft_limit_pct: float
    tokens_remaining: int | None
    calls_remaining: int | None
    blocked: bool
    soft_warned: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "enforce": self.enforce,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "calls": self.calls,
            "token_budget": self.token_budget,
            "call_budget": self.call_budget,
            "soft_limit_pct": self.soft_limit_pct,
            "tokens_remaining": self.tokens_remaining,
            "calls_remaining": self.calls_remaining,
            "blocked": self.blocked,
            "soft_warned": self.soft_warned,
        }


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _state_path(settings: Settings) -> Path | None:
    raw = (settings.llm_budget_state_path or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def _load_from_disk(settings: Settings, day: str) -> None:
    global _prompt_tokens, _completion_tokens, _calls, _warned_soft
    path = _state_path(settings)
    if path is None or not path.exists():
        return
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if str(data.get("day")) != day:
        return
    _prompt_tokens = int(data.get("prompt_tokens") or 0)
    _completion_tokens = int(data.get("completion_tokens") or 0)
    _calls = int(data.get("calls") or 0)
    _warned_soft = bool(data.get("soft_warned"))


def _save_to_disk(settings: Settings, day: str) -> None:
    path = _state_path(settings)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "day": day,
                    "prompt_tokens": _prompt_tokens,
                    "completion_tokens": _completion_tokens,
                    "calls": _calls,
                    "soft_warned": _warned_soft,
                    "updated_at": datetime.now(UTC).isoformat(),
                },
                indent=2,
            )
        )
    except OSError as exc:
        logger.warning("llm_budget_state_write_failed", error=str(exc))


def _roll_day(settings: Settings) -> str:
    global _state_day, _prompt_tokens, _completion_tokens, _calls, _warned_soft
    day = _today()
    if _state_day != day:
        _state_day = day
        _prompt_tokens = 0
        _completion_tokens = 0
        _calls = 0
        _warned_soft = False
        _load_from_disk(settings, day)
    return day


def reset_llm_budget_for_tests() -> None:
    """Test helper — clear in-memory counters."""
    global _state_day, _prompt_tokens, _completion_tokens, _calls, _warned_soft
    with _lock:
        _state_day = None
        _prompt_tokens = 0
        _completion_tokens = 0
        _calls = 0
        _warned_soft = False


def snapshot_llm_budget(settings: Settings | None = None) -> LLMBudgetSnapshot:
    cfg = settings or get_settings()
    with _lock:
        day = _roll_day(cfg)
        total = _prompt_tokens + _completion_tokens
        token_budget = max(0, int(cfg.llm_daily_token_budget))
        call_budget = max(0, int(cfg.llm_daily_call_budget))
        enforce = bool(cfg.llm_budget_enforce)
        blocked = False
        if enforce:
            if token_budget > 0 and total >= token_budget:
                blocked = True
            if call_budget > 0 and _calls >= call_budget:
                blocked = True
        return LLMBudgetSnapshot(
            day=day,
            enforce=enforce,
            prompt_tokens=_prompt_tokens,
            completion_tokens=_completion_tokens,
            total_tokens=total,
            calls=_calls,
            token_budget=token_budget,
            call_budget=call_budget,
            soft_limit_pct=float(cfg.llm_budget_soft_limit_pct),
            tokens_remaining=(token_budget - total) if token_budget > 0 else None,
            calls_remaining=(call_budget - _calls) if call_budget > 0 else None,
            blocked=blocked,
            soft_warned=_warned_soft,
        )


def assert_llm_budget_allows_call(settings: Settings | None = None) -> None:
    """Raise LLMBudgetExceeded if another billable call is not allowed."""
    cfg = settings or get_settings()
    if not cfg.llm_budget_enforce:
        return
    snap = snapshot_llm_budget(cfg)
    if snap.token_budget > 0 and snap.total_tokens >= snap.token_budget:
        raise LLMBudgetExceeded(
            f"daily_token_budget_exhausted:{snap.total_tokens}/{snap.token_budget}",
            snap.to_dict(),
        )
    if snap.call_budget > 0 and snap.calls >= snap.call_budget:
        raise LLMBudgetExceeded(
            f"daily_call_budget_exhausted:{snap.calls}/{snap.call_budget}",
            snap.to_dict(),
        )


def record_llm_usage(
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    settings: Settings | None = None,
) -> LLMBudgetSnapshot:
    """Record usage from a completed API response and emit soft-limit warnings."""
    global _prompt_tokens, _completion_tokens, _calls, _warned_soft
    cfg = settings or get_settings()
    with _lock:
        day = _roll_day(cfg)
        _prompt_tokens += max(0, int(prompt_tokens))
        _completion_tokens += max(0, int(completion_tokens))
        _calls += 1
        total = _prompt_tokens + _completion_tokens
        soft_pct = float(cfg.llm_budget_soft_limit_pct)
        token_budget = max(0, int(cfg.llm_daily_token_budget))
        call_budget = max(0, int(cfg.llm_daily_call_budget))
        if not _warned_soft and soft_pct > 0:
            token_hit = token_budget > 0 and total >= int(token_budget * soft_pct)
            call_hit = call_budget > 0 and _calls >= int(call_budget * soft_pct)
            if token_hit or call_hit:
                _warned_soft = True
                logger.warning(
                    "llm_budget_soft_limit",
                    day=day,
                    total_tokens=total,
                    token_budget=token_budget,
                    calls=_calls,
                    call_budget=call_budget,
                    soft_limit_pct=soft_pct,
                )
        _save_to_disk(cfg, day)
        # refresh metrics outside lock ideally; update gauges here for simplicity
    snap = snapshot_llm_budget(cfg)
    try:
        from app.core.metrics import LLM_BUDGET_BLOCKED, LLM_CALLS_TODAY, LLM_TOKENS_TODAY

        LLM_TOKENS_TODAY.set(snap.total_tokens)
        LLM_CALLS_TODAY.set(snap.calls)
        LLM_BUDGET_BLOCKED.set(1.0 if snap.blocked else 0.0)
    except Exception:  # noqa: BLE001
        pass
    return snap


def usage_from_openai_response(raw: dict[str, Any] | None) -> tuple[int, int]:
    if not isinstance(raw, dict):
        return 0, 0
    usage = raw.get("usage") or {}
    if not isinstance(usage, dict):
        return 0, 0
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    return prompt, completion
