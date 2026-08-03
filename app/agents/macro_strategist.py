"""Macro & Policy Strategist agent."""

from __future__ import annotations

from datetime import UTC, datetime

from app.agents.base import BaseAgent, dump_for_prompt
from app.schemas.common import AgentName, MarketRegime, TraceMetadata
from app.schemas.macro_strategist import MacroStrategistInput, MacroStrategistOutput


class MacroStrategistAgent(BaseAgent[MacroStrategistInput, MacroStrategistOutput]):
    name = AgentName.MACRO_STRATEGIST
    prompt_file = "system_v1.md"
    prompt_version = "1.0.0"

    def output_model(self) -> type[MacroStrategistOutput]:
        return MacroStrategistOutput

    def build_user_prompt(self, payload: MacroStrategistInput) -> str:
        return (
            "Classify the macro regime from this data. Return MacroStrategistOutput JSON.\n\n"
            f"{dump_for_prompt(payload)}"
        )

    def fallback_output(
        self, payload: MacroStrategistInput, *, reason: str
    ) -> MacroStrategistOutput:
        m = payload.macro
        bullish: list[str] = []
        bearish: list[str] = []
        score = 0

        if m.cpi_yoy is not None:
            if m.cpi_yoy <= 3.0:
                bullish.append(f"CPI YoY {m.cpi_yoy}% contained")
                score += 1
            else:
                bearish.append(f"CPI YoY {m.cpi_yoy}% elevated")
                score -= 1
        if m.us_10y_yield is not None and m.us_2y_yield is not None:
            curve = m.us_10y_yield - m.us_2y_yield
            if curve > 0:
                bullish.append(f"Curve steepness {curve:.2f}")
                score += 1
            else:
                bearish.append(f"Curve inverted/flat {curve:.2f}")
                score -= 1
        if m.hy_credit_spread_bps is not None:
            if m.hy_credit_spread_bps < 400:
                bullish.append(f"HY spread {m.hy_credit_spread_bps} bps manageable")
                score += 1
            else:
                bearish.append(f"HY spread {m.hy_credit_spread_bps} bps wide")
                score -= 1
        if m.unemployment_rate is not None and m.unemployment_rate > 4.5:
            bearish.append(f"Unemployment {m.unemployment_rate}% rising risk")
            score -= 1

        if score >= 2:
            regime = MarketRegime.RISK_ON
        elif score <= -2:
            regime = MarketRegime.RISK_OFF
        else:
            regime = MarketRegime.NEUTRAL

        present = sum(
            1
            for v in [m.fed_funds_rate, m.cpi_yoy, m.us_10y_yield, m.dxy, m.unemployment_rate]
            if v is not None
        )
        quality = round(0.4 + 0.12 * present, 2)

        return MacroStrategistOutput(
            timestamp=datetime.now(UTC),
            market_regime=regime,
            confidence=min(0.75, 0.4 + 0.1 * abs(score)),
            bullish_factors=bullish or ["Insufficient bullish signals"],
            bearish_factors=bearish or ["Insufficient bearish signals"],
            expected_sector_impact=[],
            invalidation_conditions=[
                "Unexpected hawkish Fed surprise",
                "Credit spreads gap wider by >50bps",
            ],
            data_quality_score=quality,
            conflicts=[],
            trace=TraceMetadata(
                agent_version=self.agent_version,
                prompt_version=self.prompt_version,
                model_name="fallback-rules",
                source_data_timestamp=payload.as_of,
                source_names=["macro_snapshot"],
            ),
        )
