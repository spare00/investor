"""Per-agent split: Python owns facts, LLM owns a single judgment.

Local and cloud are different budgets, switched only by ``llm_is_local()``.

Local ($0, one GPU): the tape in the brief is uncapped — tokens are free.
Answers stay short, Quant and Risk skip chat, weekday calls are one shot, and
the job cap is 8 minutes so 14B finishes instead of falling back.

Cloud (billable): every judgment role calls the model. Decision roles get a
longer timeout. Reasoning models (gpt-5, o-series) get the full output cap on
every role, because reasoning tokens share that cap and a short cap comes back
empty. A failed cloud call is not replaced by the local rules brain.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings
from app.schemas.common import AgentName


@dataclass(frozen=True, slots=True)
class AgentRole:
    """What this agent is allowed to spend GPU on."""

    python_owns: str
    ai_owns: str
    # Local only: skip the chat call and use the Python fallback/engine.
    skip_llm_when_local: bool
    num_ctx: int
    max_tokens: int
    # "decision" = main local model (14B). "fast" = optional smaller model.
    model_slot: str = "fast"
    # Weekend jobs may spend a validation repair round on local 14B.
    allow_local_repair: bool = False

    def skip_llm(self, settings: Settings) -> bool:
        return bool(self.skip_llm_when_local and settings.llm_is_local())

    def model_name(self, settings: Settings) -> str:
        if not settings.llm_is_local():
            return settings.llm_model
        if self.model_slot == "fast":
            fast = (settings.llm_local_fast_model or "").strip()
            if fast:
                return fast
        return settings.llm_model

    def num_ctx_for(self, settings: Settings) -> int:
        if not settings.llm_is_local():
            return 0
        # One window for every local role. Do not re-apply the old per-role
        # 4k/8k thrift cap — local context is not billed.
        return max(1, int(settings.llm_local_num_ctx))

    def max_tokens_for(self, settings: Settings) -> int:
        if settings.llm_is_local():
            # Short answers. Decode time is what blows the 8-minute cap.
            cap = max(64, int(settings.llm_local_max_tokens))
            return min(self.max_tokens, cap)
        # Cloud output includes reasoning tokens. Decision slots keep the
        # full cap. Non-reasoning fast slots stay smaller. Reasoning models
        # spend the same cap on hidden reasoning, so a 1024 ceiling returns
        # an empty message once that budget is gone.
        cap = max(256, int(settings.llm_max_tokens))
        if self.model_slot == "decision" or _reasoning_output_shares_cap(settings):
            return cap
        return min(cap, 1024)

    def timeout_seconds_for(self, settings: Settings) -> int:
        if not settings.llm_is_local():
            base = max(1, int(settings.llm_timeout_seconds))
            if self.model_slot == "decision":
                return max(base, int(settings.llm_cloud_decision_timeout_seconds))
            return base
        if self.allow_local_repair:
            return max(1, int(settings.llm_local_universe_timeout_seconds))
        return max(1, int(settings.llm_local_timeout_seconds))

    def repair_attempts_for(self, settings: Settings) -> int:
        if settings.llm_is_local() and not self.allow_local_repair:
            return 1
        return 2


def _reasoning_output_shares_cap(settings: Settings) -> bool:
    """GPT-5 and o-series count reasoning against max_completion_tokens."""
    if settings.llm_is_local():
        return False
    name = (settings.llm_model or "").strip().lower()
    return name.startswith(("gpt-5", "o1", "o3", "o4"))


# Context sizes assume the compact QUESTION/DATA/ANSWER briefs, not full dumps.
ROLES: dict[AgentName, AgentRole] = {
    AgentName.MARKET_INTELLIGENCE: AgentRole(
        python_owns="news fetch, dedupe, symbol tagging, watch/allow lists",
        ai_owns="cluster events, importance 1-5, themes",
        skip_llm_when_local=False,
        num_ctx=4096,
        max_tokens=500,
        model_slot="fast",
    ),
    AgentName.MACRO_STRATEGIST: AgentRole(
        python_owns="rates/CPI/curve/DXY/credit snapshot",
        ai_owns="one market_regime label + short bull/bear facts",
        skip_llm_when_local=False,
        num_ctx=4096,
        max_tokens=400,
        model_slot="fast",
    ),
    AgentName.QUANT_STRATEGIST: AgentRole(
        python_owns="OHLCV, SMA/RSI/ATR, trend/momentum rules, horizon stops",
        ai_owns="unused locally — Python fallback is the tape reader",
        skip_llm_when_local=True,
        num_ctx=8192,
        max_tokens=700,
        model_slot="decision",
    ),
    AgentName.RISK_MANAGER: AgentRole(
        python_owns="deterministic risk engine, live-price veto, size caps",
        ai_owns="unused locally — engine verdict is authoritative",
        skip_llm_when_local=True,
        num_ctx=4096,
        max_tokens=300,
        model_slot="fast",
    ),
    AgentName.DEVILS_ADVOCATE: AgentRole(
        python_owns="theses from Quant, compact upstream summaries",
        ai_owns="prefer_no_trade true/false and one counterpoint",
        skip_llm_when_local=False,
        num_ctx=4096,
        max_tokens=400,
        model_slot="fast",
    ),
    AgentName.CIO: AgentRole(
        python_owns="positions, allowlist, stop enrichment, horizon align",
        ai_owns="one portfolio_action and per-symbol HOLD/SELL/BUY",
        skip_llm_when_local=False,
        num_ctx=8192,
        max_tokens=700,
        model_slot="decision",
    ),
    AgentName.UNIVERSE_MANAGER: AgentRole(
        python_owns="membership pool, sectors, holdings, outcome stats, limits",
        ai_owns="industry selection then ~10 working names from reconstituted watch",
        skip_llm_when_local=False,
        num_ctx=8192,
        max_tokens=700,
        model_slot="decision",
        allow_local_repair=True,
    ),
}


def role_for(name: AgentName) -> AgentRole:
    return ROLES[name]


def roles_snapshot(settings: Settings) -> dict[str, dict[str, object]]:
    """Operator-facing split of Python vs LLM work per agent."""
    return {
        name.value: {
            "python_owns": role.python_owns,
            "ai_owns": role.ai_owns,
            "skip_llm": role.skip_llm(settings),
            "model": role.model_name(settings),
            "model_slot": role.model_slot,
            "num_ctx": role.num_ctx_for(settings),
            "max_tokens": role.max_tokens_for(settings),
            "timeout_seconds": role.timeout_seconds_for(settings),
            "repair_attempts": role.repair_attempts_for(settings),
        }
        for name, role in ROLES.items()
    }
