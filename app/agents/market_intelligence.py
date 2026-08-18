"""Market Intelligence Analyst agent."""

from __future__ import annotations

from datetime import UTC, datetime

from app.agents.base import BaseAgent
from app.agents.briefs import market_intelligence_brief
from app.schemas.common import AgentName, NewsCategory, Sentiment, TraceMetadata
from app.schemas.market_intelligence import (
    MarketEvent,
    MarketIntelligenceInput,
    MarketIntelligenceOutput,
)


class MarketIntelligenceAgent(BaseAgent[MarketIntelligenceInput, MarketIntelligenceOutput]):
    name = AgentName.MARKET_INTELLIGENCE
    prompt_file = "system_v1.md"
    prompt_version = "2.0.0"

    def output_model(self) -> type[MarketIntelligenceOutput]:
        return MarketIntelligenceOutput

    def build_user_prompt(self, payload: MarketIntelligenceInput) -> str:
        return market_intelligence_brief(payload)

    def fallback_output(
        self, payload: MarketIntelligenceInput, *, reason: str
    ) -> MarketIntelligenceOutput:
        events: list[MarketEvent] = []
        for item in payload.news_items[:20]:
            category = NewsCategory.OTHER
            hl = item.headline.lower()
            if "fed" in hl or "fomc" in hl:
                category = NewsCategory.FED
            elif "cpi" in hl or "pce" in hl or "gdp" in hl:
                category = NewsCategory.MACRO
            elif "earn" in hl:
                category = NewsCategory.EARNINGS
            events.append(
                MarketEvent(
                    headline=item.headline,
                    source=item.source,
                    published_at=item.published_at,
                    symbols=[s.upper() for s in item.symbols],
                    category=category,
                    importance=3,
                    sentiment=Sentiment.NEUTRAL,
                    facts=[f"Reported by {item.source}"],
                    uncertainties=["Automated fallback — LLM unavailable"],
                    interpretation=None,
                )
            )
        quality = 0.55 if events else 0.3
        return MarketIntelligenceOutput(
            timestamp=datetime.now(UTC),
            market_events=events,
            top_market_themes=["fallback_summary"],
            data_quality_score=quality,
            conflicts=[],
            missing_information=["LLM analysis unavailable"] if reason else [],
            trace=TraceMetadata(
                agent_version=self.agent_version,
                prompt_version=self.prompt_version,
                model_name="fallback-rules",
                source_names=[i.provider for i in payload.news_items],
                source_data_timestamp=payload.as_of,
            ),
        )
