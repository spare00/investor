"""Daily LLM budget hard-stop tests."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.core.config import Settings, clear_settings_cache
from app.services.llm import LLMError, OpenAICompatibleClient
from app.services.llm_budget import (
    assert_llm_budget_allows_call,
    record_llm_usage,
    reset_llm_budget_for_tests,
    snapshot_llm_budget,
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    clear_settings_cache()
    reset_llm_budget_for_tests()
    yield
    reset_llm_budget_for_tests()
    clear_settings_cache()


def test_budget_blocks_after_call_limit(tmp_path) -> None:
    settings = Settings(
        llm_budget_enforce=True,
        llm_daily_token_budget=1_000_000,
        llm_daily_call_budget=2,
        llm_budget_soft_limit_pct=0.5,
        llm_budget_state_path=str(tmp_path / "budget.json"),
        llm_api_key=SecretStr("sk-test"),
    )
    record_llm_usage(prompt_tokens=10, completion_tokens=5, settings=settings)
    record_llm_usage(prompt_tokens=10, completion_tokens=5, settings=settings)
    snap = snapshot_llm_budget(settings)
    assert snap.calls == 2
    assert snap.blocked is True
    with pytest.raises(Exception, match="call_budget"):
        assert_llm_budget_allows_call(settings)


def test_budget_blocks_after_token_limit(tmp_path) -> None:
    settings = Settings(
        llm_budget_enforce=True,
        llm_daily_token_budget=100,
        llm_daily_call_budget=1000,
        llm_budget_state_path=str(tmp_path / "budget.json"),
        llm_api_key=SecretStr("sk-test"),
    )
    record_llm_usage(prompt_tokens=60, completion_tokens=50, settings=settings)
    snap = snapshot_llm_budget(settings)
    assert snap.total_tokens == 110
    assert snap.blocked is True


def test_budget_disabled_allows_overage(tmp_path) -> None:
    settings = Settings(
        llm_budget_enforce=False,
        llm_daily_token_budget=1,
        llm_daily_call_budget=1,
        llm_budget_state_path=str(tmp_path / "budget.json"),
    )
    record_llm_usage(prompt_tokens=500, completion_tokens=500, settings=settings)
    assert_llm_budget_allows_call(settings)  # no raise
    assert snapshot_llm_budget(settings).blocked is False


@pytest.mark.asyncio
async def test_client_raises_llm_error_when_budget_exhausted(tmp_path) -> None:
    settings = Settings(
        llm_budget_enforce=True,
        llm_daily_token_budget=10,
        llm_daily_call_budget=100,
        llm_budget_state_path=str(tmp_path / "budget.json"),
        llm_api_key=SecretStr("sk-test"),
    )
    record_llm_usage(prompt_tokens=20, completion_tokens=0, settings=settings)
    client = OpenAICompatibleClient(settings)
    with pytest.raises(LLMError, match="token_budget"):
        await client.complete_json(system_prompt="s", user_prompt="u")
