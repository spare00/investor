"""Per-agent split: Python owns facts, LLM owns a single judgment.

Local 14B must finish inside the 8-minute job cap. That means:
- Python computes indicators, risk vetoes, compact briefs.
- LLM is called only where a human would still have to choose.
- Each agent has its own context window, max tokens, and model slot.
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
        return min(self.num_ctx, max(1, int(settings.llm_local_num_ctx)))

    def max_tokens_for(self, settings: Settings) -> int:
        if settings.llm_is_local():
            cap = max(64, int(settings.llm_local_max_tokens))
            return min(self.max_tokens, cap)
        return settings.llm_max_tokens


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
        ai_owns="industry selection then keep/pause/add; pick ~10 working names",
        skip_llm_when_local=False,
        num_ctx=8192,
        max_tokens=700,
        model_slot="decision",
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
        }
        for name, role in ROLES.items()
    }
