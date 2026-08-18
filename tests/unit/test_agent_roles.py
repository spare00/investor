"""Python vs LLM ownership and per-agent local context/model."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.agents.roles import ROLES, role_for, roles_snapshot
from app.agents.quant_strategist import QuantStrategistAgent
from app.agents.risk_manager import RiskManagerAgent
from app.core.config import Settings
from app.schemas.common import AgentName
from app.schemas.quant_strategist import BarSnapshot, QuantStrategistInput
from app.schemas.risk_manager import PortfolioStateInput, RiskManagerInput
from app.services.llm import StubLLMClient

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def test_every_agent_has_a_role() -> None:
    assert set(ROLES) == set(AgentName)


def test_local_skips_quant_and_risk_only() -> None:
    local = Settings(llm_runtime="local", llm_api_key=None)
    cloud = Settings(
        llm_runtime="cloud",
        llm_base_url="https://api.openai.com/v1",
        llm_api_key=None,
    )
    skipped = {name for name, role in ROLES.items() if role.skip_llm(local)}
    assert skipped == {AgentName.QUANT_STRATEGIST, AgentName.RISK_MANAGER}
    assert not any(role.skip_llm(cloud) for role in ROLES.values())


def test_local_cio_uses_8k_decision_slot_capped_by_settings() -> None:
    settings = Settings(
        llm_runtime="local",
        llm_api_key=None,
        llm_local_model="qwen2.5:14b-ctx",
        llm_local_fast_model="qwen2.5:7b",
        llm_local_num_ctx=8192,
        llm_local_max_tokens=800,
    )
    cio = role_for(AgentName.CIO)
    mi = role_for(AgentName.MARKET_INTELLIGENCE)
    assert cio.model_name(settings) == "qwen2.5:14b-ctx"
    assert mi.model_name(settings) == "qwen2.5:7b"
    assert cio.num_ctx_for(settings) == 8192
    assert mi.num_ctx_for(settings) == 4096
    assert cio.max_tokens_for(settings) == 700
    assert mi.max_tokens_for(settings) == 500


def test_roles_snapshot_marks_python_vs_ai() -> None:
    settings = Settings(llm_runtime="local", llm_api_key=None)
    snap = roles_snapshot(settings)
    assert snap["quant_strategist"]["skip_llm"] is True
    assert "OHLCV" in snap["quant_strategist"]["python_owns"]
    assert snap["cio"]["skip_llm"] is False
    assert snap["cio"]["model_slot"] == "decision"


@pytest.mark.asyncio
async def test_local_quant_never_calls_llm() -> None:
    settings = Settings(llm_runtime="local", llm_api_key=None)
    stub = StubLLMClient(payload={})
    out = await QuantStrategistAgent(llm=stub, settings=settings).run(
        QuantStrategistInput(
            as_of=NOW,
            symbol_bars=[
                BarSnapshot(symbol="BHP", last=42.0, sma_50=40.0, sma_200=38.0, rsi_14=58.0)
            ],
        )
    )
    assert stub.calls == []
    assert out.trace.model_name == "python-rules"
    assert out.symbol_views


@pytest.mark.asyncio
async def test_local_risk_never_calls_llm() -> None:
    settings = Settings(llm_runtime="local", llm_api_key=None)
    stub = StubLLMClient(payload={})
    out = await RiskManagerAgent(llm=stub, settings=settings).run(
        RiskManagerInput(
            as_of=NOW,
            portfolio=PortfolioStateInput(
                as_of=NOW,
                equity=25_000,
                cash=20_000,
                cash_pct=80,
                gross_exposure_pct=20,
            ),
            proposed_trades=[],
        )
    )
    assert stub.calls == []
    assert out.trace.model_name == "risk-engine"
    assert "Python risk engine" in (out.soft_warnings or [""])[0]


@pytest.mark.asyncio
async def test_local_pipeline_quant_and_risk_are_python() -> None:
    from uuid import uuid4

    from app.agents import AgentPipeline
    from app.services.collection import CollectionBundle
    from app.services.normalize import NormalizedMacroSnapshot, NormalizedMarketSnapshot, NormalizedNews

    settings = Settings(llm_runtime="local", llm_api_key=None)
    stub = StubLLMClient(payload={})
    collection = CollectionBundle(
        workflow_id=uuid4(),
        collected_at=NOW,
        news=[
            NormalizedNews(
                provider="stub",
                external_id="1",
                headline="Fed patience",
                headline_hash="abc",
                source="Reuters",
                url=None,
                published_at=NOW,
                collected_at=NOW,
                symbols=["SPY"],
                category="fed",
                raw_payload={},
                freshness_score=0.9,
                quality_score=0.9,
            )
        ],
        markets=[
            NormalizedMarketSnapshot(
                symbol="SPY",
                as_of=NOW,
                provider="stub",
                last=560,
                open=558,
                high=562,
                low=557,
                volume=1e7,
                avg_volume_20d=5e7,
                atr_14=8,
                rsi_14=55,
                sma_20=555,
                sma_50=550,
                sma_200=520,
                bid=559.9,
                ask=560.1,
                spread_bps=3.5,
                premarket_change_pct=0.2,
                gap_pct=0.1,
                vix=16.0,
                raw_payload={},
                freshness_score=0.95,
                quality_score=0.9,
            )
        ],
        macro=NormalizedMacroSnapshot(
            as_of=NOW,
            provider="stub",
            fed_funds_rate=5.25,
            cpi_yoy=2.8,
            pce_yoy=2.5,
            unemployment_rate=4.1,
            gdp_growth_q_o_q=2.0,
            us_10y_yield=4.2,
            us_2y_yield=4.0,
            dxy=104.0,
            wti_oil=78.0,
            gold=2300.0,
            hy_credit_spread_bps=310.0,
            notes=[],
            raw_payload={},
            freshness_score=0.9,
            quality_score=0.85,
        ),
        aggregate_quality=0.9,
    )
    result = await AgentPipeline(settings=settings, llm=stub).run_from_collection(
        collection,
        portfolio=PortfolioStateInput(
            as_of=NOW,
            equity=25_000,
            cash=20_000,
            cash_pct=80,
            gross_exposure_pct=20,
        ),
        proposed_trades=[],
    )
    assert result.quant.trace.model_name == "python-rules"
    assert result.risk.trace.model_name == "risk-engine"
    # MI + Macro + Devil + CIO only (Quant/Risk skipped). Local: one attempt each.
    assert len(stub.calls) == 4
    assert stub.calls[0]["num_ctx"] == "4096"
    assert stub.calls[-1]["num_ctx"] == "8192"  # CIO decision slot
    assert stub.calls[-1]["max_tokens"] == "700"
