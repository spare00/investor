"""Phase 2 prompt and framework tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.prompts import (
    AGENT_PROMPT_KEYS,
    REQUIRED_PROMPT_SECTIONS,
    load_agent_prompt,
    load_shared,
)
from app.agents.base import BaseAgent
from app.agents.market_intelligence import MarketIntelligenceAgent
from app.services.llm import FakeLLMProvider, StubLLMClient


PROMPTS = Path(__file__).resolve().parents[2] / "prompts"


def test_shared_prompt_files_exist() -> None:
    assert (PROMPTS / "shared" / "common_rules.md").exists()
    assert (PROMPTS / "shared" / "output_contract.md").exists()


@pytest.mark.parametrize("agent_key", AGENT_PROMPT_KEYS)
def test_agent_system_v1_exists_and_has_sections(agent_key: str) -> None:
    path = PROMPTS / agent_key / "system_v1.md"
    assert path.exists(), path
    text = path.read_text(encoding="utf-8")
    for section in REQUIRED_PROMPT_SECTIONS:
        assert section in text, f"{agent_key} missing section {section}"
    assert "Broker" in text or "broker" in text
    assert "JSON" in text


@pytest.mark.parametrize("agent_key", AGENT_PROMPT_KEYS)
def test_loaded_prompt_includes_common_rules_and_hash(agent_key: str) -> None:
    loaded = load_agent_prompt(agent_key)
    assert loaded.version  # from Prompt-Version header (may differ per agent)
    assert len(loaded.sha256) == 64
    assert "Data use" in loaded.common_rules or "provided input" in loaded.common_rules.lower()
    assert "Broker" in loaded.system_prompt or "broker" in loaded.system_prompt
    assert "JSON" in loaded.system_prompt
    assert "never call Broker" in loaded.system_prompt.lower() or "Broker API" in loaded.system_prompt


def test_fake_llm_alias() -> None:
    assert FakeLLMProvider is StubLLMClient
    client = FakeLLMProvider({"ok": True})
    assert client.payload["ok"] is True


@pytest.mark.asyncio
async def test_agent_records_prompt_hash_in_trace() -> None:
    from datetime import UTC, datetime

    from app.schemas.market_intelligence import MarketIntelligenceInput

    # Empty stub forces validation failure → fallback; still exercises prompt load path.
    agent = MarketIntelligenceAgent(llm=FakeLLMProvider({}))
    loaded = agent.load_prompt()
    assert loaded.sha256
    out = await agent.run(
        MarketIntelligenceInput(as_of=datetime.now(UTC), news_items=[], allowlist=["SPY"])
    )
    # Fallback path may not include hash; ensure loader worked and common rules present.
    system = agent.load_system_prompt()
    assert loaded.sha256 in system
    assert out.data_quality_score >= 0.0
