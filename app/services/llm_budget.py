"""LLM spend budget — monthly AUD is primary; daily token/call caps derive from it.

Counters are process-local with an optional JSON state file. Daily/monthly keys
follow the operator calendar (``OPERATOR_TIMEZONE``, default Australia/Brisbane).
Event timestamps (``updated_at``) stay UTC. Cost is estimated from
prompt/completion tokens using configured per-1M USD rates and AUD/USD.

When ``llm_daily_token_budget`` / ``llm_daily_call_budget`` are 0, daily caps are
sliced from ``llm_monthly_aud_budget / trading_days`` using model $/token rates.
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
from app.core.timeutils import operator_calendar_day_iso, operator_calendar_month

logger = get_logger(__name__)

_lock = threading.Lock()
_state_day: str | None = None
_state_month: str | None = None
_prompt_tokens = 0
_completion_tokens = 0
_calls = 0
_month_prompt_tokens = 0
_month_completion_tokens = 0
_month_calls = 0
_warned_soft = False
_month_warned_soft = False
_last_loaded_mtime: float | None = None


class LLMBudgetExceeded(Exception):
    """Raised when a daily or monthly LLM budget would be exceeded."""

    def __init__(self, reason: str, snapshot: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.snapshot = snapshot or {}


@dataclass(frozen=True, slots=True)
class DailyBudgetResolution:
    token_budget: int
    call_budget: int
    daily_aud_budget: float
    derived: bool
    trading_days: int


def resolve_daily_llm_budgets(settings: Settings) -> DailyBudgetResolution:
    """Resolve effective daily token/call caps.

    Monthly AUD is the source of truth. With daily overrides at 0, split the
    month across ``llm_budget_trading_days_per_month`` and invert model rates
    to a token (and call) cap. Explicit daily >0 keeps manual/test overrides.
    """
    month_aud = max(0.0, float(settings.llm_monthly_aud_budget))
    days = max(1, int(settings.llm_budget_trading_days_per_month or 21))
    explicit_tok = max(0, int(settings.llm_daily_token_budget))
    explicit_call = max(0, int(settings.llm_daily_call_budget))

    if month_aud <= 0:
        return DailyBudgetResolution(
            token_budget=explicit_tok,
            call_budget=explicit_call,
            daily_aud_budget=0.0,
            derived=False,
            trading_days=days,
        )

    daily_aud = month_aud / float(days)
    derived_tok = 0
    derived_call = 0
    aud_per_usd = max(1e-9, float(settings.llm_aud_per_usd))
    daily_usd = daily_aud / aud_per_usd
    share = min(1.0, max(0.0, float(settings.llm_budget_input_token_share)))
    blend = share * float(settings.llm_input_usd_per_mtok) + (1.0 - share) * float(
        settings.llm_output_usd_per_mtok
    )
    if blend > 0 and daily_usd > 0:
        derived_tok = max(1, int(daily_usd / blend * 1_000_000))
        avg_call = max(1, int(settings.llm_budget_avg_tokens_per_call or 5_000))
        derived_call = max(1, derived_tok // avg_call)

    token_budget = explicit_tok if explicit_tok > 0 else derived_tok
    call_budget = explicit_call if explicit_call > 0 else derived_call
    derived = explicit_tok <= 0 or explicit_call <= 0
    return DailyBudgetResolution(
        token_budget=token_budget,
        call_budget=call_budget,
        daily_aud_budget=daily_aud,
        derived=derived and month_aud > 0,
        trading_days=days,
    )


@dataclass(frozen=True, slots=True)
class LLMBudgetSnapshot:
    day: str
    month: str
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
    month_prompt_tokens: int
    month_completion_tokens: int
    month_total_tokens: int
    month_calls: int
    month_usd_estimate: float
    month_aud_estimate: float
    month_aud_budget: float
    month_aud_remaining: float | None
    month_blocked: bool
    month_soft_warned: bool
    daily_aud_budget: float = 0.0
    daily_budget_derived: bool = False
    trading_days_per_month: int = 21

    def to_dict(self) -> dict[str, Any]:
        daily_pct = 0.0
        if self.token_budget > 0:
            daily_pct = max(daily_pct, self.total_tokens / self.token_budget)
        if self.call_budget > 0:
            daily_pct = max(daily_pct, self.calls / self.call_budget)
        month_pct = 0.0
        if self.month_aud_budget > 0:
            month_pct = self.month_aud_estimate / self.month_aud_budget
        return {
            "day": self.day,
            "month": self.month,
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
            "month_prompt_tokens": self.month_prompt_tokens,
            "month_completion_tokens": self.month_completion_tokens,
            "month_total_tokens": self.month_total_tokens,
            "month_calls": self.month_calls,
            "month_usd_estimate": round(self.month_usd_estimate, 4),
            "month_aud_estimate": round(self.month_aud_estimate, 4),
            "month_aud_budget": self.month_aud_budget,
            "month_aud_remaining": (
                None
                if self.month_aud_remaining is None
                else round(self.month_aud_remaining, 4)
            ),
            "month_blocked": self.month_blocked,
            "month_soft_warned": self.month_soft_warned,
            "daily_aud_budget": round(self.daily_aud_budget, 4),
            "daily_budget_derived": self.daily_budget_derived,
            "trading_days_per_month": self.trading_days_per_month,
            "daily_pct": round(daily_pct, 4),
            "month_pct": round(month_pct, 4),
            "display_pct": round(max(daily_pct, month_pct), 4),
        }


def _today(settings: Settings | None = None) -> str:
    return operator_calendar_day_iso(settings)


def _month(settings: Settings | None = None) -> str:
    return operator_calendar_month(settings)


def estimate_usd_cost(
    prompt_tokens: int,
    completion_tokens: int,
    settings: Settings,
) -> float:
    inp = max(0, int(prompt_tokens)) / 1_000_000.0 * float(settings.llm_input_usd_per_mtok)
    out = max(0, int(completion_tokens)) / 1_000_000.0 * float(settings.llm_output_usd_per_mtok)
    return inp + out


def estimate_aud_cost(
    prompt_tokens: int,
    completion_tokens: int,
    settings: Settings,
) -> float:
    return estimate_usd_cost(prompt_tokens, completion_tokens, settings) * float(
        settings.llm_aud_per_usd
    )


def _state_path(settings: Settings) -> Path | None:
    raw = (settings.llm_budget_state_path or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def _load_from_disk(settings: Settings, day: str, month: str, *, merge: bool = False) -> None:
    global _prompt_tokens, _completion_tokens, _calls, _warned_soft
    global _month_prompt_tokens, _month_completion_tokens, _month_calls, _month_warned_soft
    global _last_loaded_mtime
    path = _state_path(settings)
    if path is None or not path.exists():
        return
    try:
        mtime = path.stat().st_mtime
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    file_day = str(data.get("day") or "")
    file_month = str(data.get("month") or "")
    if not file_month and file_day:
        file_month = file_day[:7]

    def _take(current: int, incoming: int) -> int:
        return max(current, incoming) if merge else incoming

    if file_day == day:
        _prompt_tokens = _take(_prompt_tokens, int(data.get("prompt_tokens") or 0))
        _completion_tokens = _take(_completion_tokens, int(data.get("completion_tokens") or 0))
        _calls = _take(_calls, int(data.get("calls") or 0))
        if data.get("soft_warned"):
            _warned_soft = True
    if file_month == month:
        # Prefer explicit month counters; legacy files without month_* only seed on cold start.
        if "month_prompt_tokens" in data or "month_calls" in data:
            mp = int(data.get("month_prompt_tokens") or 0)
            mc = int(data.get("month_completion_tokens") or 0)
            mcalls = int(data.get("month_calls") or 0)
        elif (
            not merge
            and _month_prompt_tokens == 0
            and _month_completion_tokens == 0
            and _month_calls == 0
        ):
            mp = int(data.get("prompt_tokens") or 0)
            mc = int(data.get("completion_tokens") or 0)
            mcalls = int(data.get("calls") or 0)
        else:
            mp = mc = mcalls = None
        if mp is not None and mc is not None and mcalls is not None:
            _month_prompt_tokens = _take(_month_prompt_tokens, mp)
            _month_completion_tokens = _take(_month_completion_tokens, mc)
            _month_calls = _take(_month_calls, mcalls)
            if data.get("month_soft_warned") or data.get("soft_warned"):
                _month_warned_soft = True
    _last_loaded_mtime = mtime


def _save_to_disk(settings: Settings, day: str, month: str) -> None:
    global _last_loaded_mtime
    path = _state_path(settings)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "day": day,
                    "month": month,
                    "prompt_tokens": _prompt_tokens,
                    "completion_tokens": _completion_tokens,
                    "calls": _calls,
                    "soft_warned": _warned_soft,
                    "month_prompt_tokens": _month_prompt_tokens,
                    "month_completion_tokens": _month_completion_tokens,
                    "month_calls": _month_calls,
                    "month_soft_warned": _month_warned_soft,
                    "updated_at": datetime.now(UTC).isoformat(),
                },
                indent=2,
            )
        )
        _last_loaded_mtime = path.stat().st_mtime
    except OSError as exc:
        logger.warning("llm_budget_state_write_failed", error=str(exc))


def _sync_disk_if_newer(settings: Settings, day: str, month: str) -> None:
    """Pick up counters written by another process/worker."""
    global _last_loaded_mtime
    path = _state_path(settings)
    if path is None or not path.exists():
        return
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return
    if _last_loaded_mtime is not None and mtime <= _last_loaded_mtime:
        return
    _load_from_disk(settings, day, month, merge=True)


def _roll_period(settings: Settings) -> tuple[str, str]:
    global _state_day, _state_month
    global _prompt_tokens, _completion_tokens, _calls, _warned_soft
    global _month_prompt_tokens, _month_completion_tokens, _month_calls, _month_warned_soft
    day = _today(settings)
    month = _month(settings)
    if _state_day != day or _state_month != month:
        prev_day, prev_month = _state_day, _state_month
        if _state_day != day:
            _prompt_tokens = 0
            _completion_tokens = 0
            _calls = 0
            _warned_soft = False
        if _state_month != month:
            _month_prompt_tokens = 0
            _month_completion_tokens = 0
            _month_calls = 0
            _month_warned_soft = False
        _state_day = day
        _state_month = month
        # Load disk after reset so same-day/month restart restores counters.
        if prev_day is None or prev_month is None or prev_day != day or prev_month != month:
            _load_from_disk(settings, day, month, merge=False)
    else:
        _sync_disk_if_newer(settings, day, month)
    return day, month


def reset_llm_budget_for_tests() -> None:
    """Test helper — clear in-memory counters."""
    global _state_day, _state_month
    global _prompt_tokens, _completion_tokens, _calls, _warned_soft
    global _month_prompt_tokens, _month_completion_tokens, _month_calls, _month_warned_soft
    global _last_loaded_mtime
    with _lock:
        _state_day = None
        _state_month = None
        _prompt_tokens = 0
        _completion_tokens = 0
        _calls = 0
        _warned_soft = False
        _month_prompt_tokens = 0
        _month_completion_tokens = 0
        _month_calls = 0
        _month_warned_soft = False
        _last_loaded_mtime = None


def snapshot_llm_budget(settings: Settings | None = None) -> LLMBudgetSnapshot:
    cfg = settings or get_settings()
    with _lock:
        day, month = _roll_period(cfg)
        total = _prompt_tokens + _completion_tokens
        month_total = _month_prompt_tokens + _month_completion_tokens
        daily = resolve_daily_llm_budgets(cfg)
        token_budget = daily.token_budget
        call_budget = daily.call_budget
        month_aud_budget = max(0.0, float(cfg.llm_monthly_aud_budget))
        month_usd = estimate_usd_cost(_month_prompt_tokens, _month_completion_tokens, cfg)
        month_aud = month_usd * float(cfg.llm_aud_per_usd)
        enforce = bool(cfg.llm_budget_enforce)
        blocked = False
        month_blocked = False
        if enforce:
            if token_budget > 0 and total >= token_budget:
                blocked = True
            if call_budget > 0 and _calls >= call_budget:
                blocked = True
            if month_aud_budget > 0 and month_aud >= month_aud_budget:
                month_blocked = True
                blocked = True
        return LLMBudgetSnapshot(
            day=day,
            month=month,
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
            month_prompt_tokens=_month_prompt_tokens,
            month_completion_tokens=_month_completion_tokens,
            month_total_tokens=month_total,
            month_calls=_month_calls,
            month_usd_estimate=month_usd,
            month_aud_estimate=month_aud,
            month_aud_budget=month_aud_budget,
            month_aud_remaining=(month_aud_budget - month_aud) if month_aud_budget > 0 else None,
            month_blocked=month_blocked,
            month_soft_warned=_month_warned_soft,
            daily_aud_budget=daily.daily_aud_budget,
            daily_budget_derived=daily.derived,
            trading_days_per_month=daily.trading_days,
        )


def assert_llm_budget_allows_call(settings: Settings | None = None) -> None:
    """Raise LLMBudgetExceeded if another billable call is not allowed."""
    cfg = settings or get_settings()
    if not cfg.llm_budget_enforce:
        return
    snap = snapshot_llm_budget(cfg)
    if snap.month_aud_budget > 0 and snap.month_aud_estimate >= snap.month_aud_budget:
        raise LLMBudgetExceeded(
            f"monthly_aud_budget_exhausted:{snap.month_aud_estimate:.2f}/{snap.month_aud_budget:.2f}",
            snap.to_dict(),
        )
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
    global _month_prompt_tokens, _month_completion_tokens, _month_calls, _month_warned_soft
    cfg = settings or get_settings()
    with _lock:
        day, month = _roll_period(cfg)
        add_p = max(0, int(prompt_tokens))
        add_c = max(0, int(completion_tokens))
        _prompt_tokens += add_p
        _completion_tokens += add_c
        _calls += 1
        _month_prompt_tokens += add_p
        _month_completion_tokens += add_c
        _month_calls += 1
        total = _prompt_tokens + _completion_tokens
        soft_pct = float(cfg.llm_budget_soft_limit_pct)
        daily = resolve_daily_llm_budgets(cfg)
        token_budget = daily.token_budget
        call_budget = daily.call_budget
        month_aud_budget = max(0.0, float(cfg.llm_monthly_aud_budget))
        month_aud = estimate_aud_cost(_month_prompt_tokens, _month_completion_tokens, cfg)
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
                    daily_budget_derived=daily.derived,
                )
        if not _month_warned_soft and soft_pct > 0 and month_aud_budget > 0:
            if month_aud >= month_aud_budget * soft_pct:
                _month_warned_soft = True
                logger.warning(
                    "llm_monthly_aud_soft_limit",
                    month=month,
                    month_aud_estimate=round(month_aud, 4),
                    month_aud_budget=month_aud_budget,
                    soft_limit_pct=soft_pct,
                )
        _save_to_disk(cfg, day, month)
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
