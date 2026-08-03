"""Devil's Advocate agent."""

from __future__ import annotations

from datetime import UTC, datetime

from app.agents.base import BaseAgent, dump_for_prompt
from app.schemas.common import AgentName, TraceMetadata
from app.schemas.devils_advocate import DevilsAdvocateInput, DevilsAdvocateOutput


class DevilsAdvocateAgent(BaseAgent[DevilsAdvocateInput, DevilsAdvocateOutput]):
    name = AgentName.DEVILS_ADVOCATE
    prompt_file = "system_v1.md"
    prompt_version = "1.0.0"

    def output_model(self) -> type[DevilsAdvocateOutput]:
        return DevilsAdvocateOutput

    def build_user_prompt(self, payload: DevilsAdvocateInput) -> str:
        return (
            "Challenge the proposed theses. Answer the five mandatory questions.\n\n"
            f"{dump_for_prompt(payload)}"
        )

    def fallback_output(
        self, payload: DevilsAdvocateInput, *, reason: str
    ) -> DevilsAdvocateOutput:
        prefer_no = False
        if payload.risk and payload.risk.halt_new_trades:
            prefer_no = True
        if payload.quant and payload.quant.market_volatility_state.value in {"elevated", "extreme"}:
            prefer_no = True

        already_in_price = False
        if payload.market_intelligence:
            for ev in payload.market_intelligence.market_events:
                if ev.importance >= 4:
                    already_in_price = True
                    break

        return DevilsAdvocateOutput(
            timestamp=datetime.now(UTC),
            strongest_reason_thesis_is_wrong=(
                "Consensus may be extrapolating incomplete premarket information"
            ),
            information_already_in_price=already_in_price,
            information_already_in_price_rationale=(
                "High-importance headlines often gap prices before the open"
                if already_in_price
                else "No high-importance headline dominance detected in fallback"
            ),
            opposing_market_scenario="Risk-off fade after open if breadth fails",
            prefer_no_trade=prefer_no,
            prefer_no_trade_rationale=(
                "Risk halt or elevated volatility favors patience"
                if prefer_no
                else "Asymmetry acceptable under fallback heuristics"
            ),
            immediate_withdrawal_conditions=[
                "Break of premarket low on index ETF",
                "VIX spike >2 points from open",
                "Risk Hard Veto triggers",
            ],
            confirmation_bias_flags=["Check for same-direction agent agreement"],
            crowd_trade_risk=bool(payload.consensus_lean),
            trap_risk="none",
            alternative_strategies=["Reduce size", "Wait for open range break"],
            missing_information=["Full Level-2 liquidity tape"],
            data_conflicts=list(
                (payload.macro.conflicts if payload.macro else [])
                + (payload.quant.conflicts if payload.quant else [])
            ),
            challenge_score=0.65 if prefer_no else 0.45,
            trace=TraceMetadata(
                agent_version=self.agent_version,
                prompt_version=self.prompt_version,
                model_name="fallback-rules",
                source_data_timestamp=payload.as_of,
            ),
        )
