"""Risk Officer owns present-market price integrity."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.agents.risk_manager import RiskManagerAgent
from app.core.config import Settings
from app.market.live_prices import assess_collection_price_integrity
from app.risk.types import VetoCode
from app.schemas.risk_manager import PortfolioStateInput, RiskManagerInput
from app.services.llm import StubLLMClient


NOW = datetime(2026, 8, 6, 16, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_risk_officer_hard_vetoes_non_live_prices() -> None:
    agent = RiskManagerAgent(
        llm=StubLLMClient(),
        settings=Settings(enable_broker_orders=True, enable_external_data=True),
    )
    out = await agent.run(
        RiskManagerInput(
            as_of=NOW,
            portfolio=PortfolioStateInput(
                as_of=NOW,
                equity=100_000,
                cash=100_000,
                cash_pct=100,
                gross_exposure_pct=0,
            ),
            proposed_trades=[],
            live_prices_required=True,
            price_feed_live=False,
            price_providers=["stub"],
            price_integrity_notes=["simulation_provider_present"],
        )
    )
    assert VetoCode.NON_LIVE_MARKET_PRICES.value in out.hard_vetoes
    assert out.halt_new_trades is True
    assert out.overall_verdict.value in {"rejected", "halt_day"}


@pytest.mark.asyncio
async def test_risk_officer_allows_live_alpaca_feed() -> None:
    agent = RiskManagerAgent(llm=StubLLMClient())
    out = await agent.run(
        RiskManagerInput(
            as_of=NOW,
            portfolio=PortfolioStateInput(
                as_of=NOW,
                equity=100_000,
                cash=100_000,
                cash_pct=100,
                gross_exposure_pct=0,
            ),
            live_prices_required=True,
            price_feed_live=True,
            price_providers=["alpaca"],
        )
    )
    assert VetoCode.NON_LIVE_MARKET_PRICES.value not in out.hard_vetoes
    assert out.overall_verdict.value == "approved"


def test_assess_collection_price_integrity_flags_stub() -> None:
    live_req, feed_live, providers, notes = assess_collection_price_integrity(
        providers=["stub"],
        market_count=3,
        settings=Settings(enable_broker_orders=True, enable_external_data=True),
    )
    assert live_req is True
    assert feed_live is False
    assert "stub" in providers
    assert notes
