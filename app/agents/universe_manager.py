"""Universe Manager agent — AI watchlist / focus maintenance."""

from __future__ import annotations

from datetime import UTC, datetime

from app.agents.base import BaseAgent
from app.agents.briefs import universe_brief
from app.schemas.common import AgentName, TraceMetadata
from app.schemas.universe_manager import (
    UniverseManagerInput,
    UniverseManagerOutput,
    WatchlistProposal,
)
from app.universe.horizons import UniverseHorizon


class UniverseManagerAgent(BaseAgent[UniverseManagerInput, UniverseManagerOutput]):
    name = AgentName.UNIVERSE_MANAGER
    prompt_file = "system_v1.md"
    prompt_version = "2.0.0"

    def output_model(self) -> type[UniverseManagerOutput]:
        return UniverseManagerOutput

    def build_user_prompt(self, payload: UniverseManagerInput) -> str:
        return universe_brief(payload)

    def fallback_output(self, payload: UniverseManagerInput, *, reason: str) -> UniverseManagerOutput:
        """Deterministic seed: keep allowlist/seed as short+day mix, focus = holdings ∪ top seed."""
        proposals: list[WatchlistProposal] = []
        seed = [s.upper() for s in payload.seed_pool] or [
            str(x.get("symbol", "")).upper() for x in payload.current_watchlist if x.get("symbol")
        ]
        indexes = {"SPY", "QQQ", "IWM", "DIA", "VAS", "IOZ", "NDQ", "JPEQ"}
        for i, sym in enumerate(seed[: payload.watchlist_limit]):
            if not sym:
                continue
            horizon = UniverseHorizon.DAY if sym in indexes else UniverseHorizon.SHORT
            if sym in {"NVDA", "TSLA", "AMD", "META"}:
                horizon = UniverseHorizon.DAY
            if sym in indexes and i < 2:
                horizon = UniverseHorizon.SCALP
            proposals.append(
                WatchlistProposal(
                    symbol=sym,
                    horizon=horizon,
                    action="keep" if any(str(w.get("symbol", "")).upper() == sym for w in payload.current_watchlist) else "add",
                    priority=max(10, 90 - i * 3),
                    thesis=f"Fallback seed for {horizon.value} book",
                    invalidation="Liquidity or thesis break",
                    rationale=f"fallback:{reason[:120]}",
                )
            )
        holdings = [h.upper() for h in payload.holdings]
        focus: list[str] = []
        for h in holdings:
            if h not in focus:
                focus.append(h)
        for p in sorted(proposals, key=lambda x: -x.priority):
            if p.symbol not in focus:
                focus.append(p.symbol)
            if len(focus) >= payload.focus_limit:
                break
        return UniverseManagerOutput(
            timestamp=datetime.now(UTC),
            proposals=proposals,
            focus_symbols=focus[: payload.focus_limit],
            focus_rationale="Fallback focus from seed + holdings",
            notes=[f"fallback:{reason[:200]}"],
            data_quality_score=0.5,
            trace=payload.trace or TraceMetadata(),
        )
