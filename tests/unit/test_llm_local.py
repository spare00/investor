"""Local/embedded LLM runtime — skip OpenAI spend caps."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.services.llm import OpenAICompatibleClient, StubLLMClient, get_llm_client
from app.services.llm_budget import (
    assert_llm_budget_allows_call,
    record_llm_usage,
    reset_llm_budget_for_tests,
    snapshot_llm_budget,
)
from app.universe.reeval import (
    effective_max_intraday_reanalyses,
    planned_intraday_interval_minutes,
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    from app.core.config import clear_settings_cache

    clear_settings_cache()
    reset_llm_budget_for_tests()
    yield
    reset_llm_budget_for_tests()
    clear_settings_cache()


def test_local_runtime_rewrites_openai_url_and_gpt_model() -> None:
    settings = Settings(
        llm_runtime="local",
        llm_base_url="https://api.openai.com/v1",
        llm_model="gpt-4o-mini",
        llm_local_base_url="http://127.0.0.1:11434/v1",
        llm_local_model="qwen2.5:14b",
        llm_api_key=None,
    )
    assert settings.llm_is_local() is True
    assert settings.llm_base_url == "http://127.0.0.1:11434/v1"
    assert settings.llm_model == "qwen2.5:14b"
    assert settings.llm_spend_budget_applies() is False


def test_loopback_url_counts_as_local_even_if_runtime_cloud() -> None:
    settings = Settings(
        llm_runtime="cloud",
        llm_base_url="http://localhost:11434/v1",
        llm_model="qwen2.5:7b",
    )
    assert settings.llm_is_local() is True


def test_local_does_not_block_when_cloud_budget_exhausted(tmp_path) -> None:
    settings = Settings(
        llm_runtime="local",
        llm_budget_enforce=True,
        llm_monthly_aud_budget=0.01,
        llm_daily_token_budget=1,
        llm_daily_call_budget=1,
        llm_budget_state_path=str(tmp_path / "budget.json"),
        llm_api_key=None,
    )
    record_llm_usage(prompt_tokens=50_000, completion_tokens=50_000, settings=settings)
    assert_llm_budget_allows_call(settings)
    snap = snapshot_llm_budget(settings)
    assert snap.blocked is False
    assert snap.enforce is False


def test_local_job_timeout_and_fake_llm_flag() -> None:
    local = Settings(llm_runtime="local", llm_api_key=None)
    cloud = Settings(
        llm_runtime="cloud",
        llm_base_url="https://api.openai.com/v1",
        llm_api_key=None,
        job_action_timeout_seconds=480,
    )
    assert local.effective_job_action_timeout_seconds() == 480
    assert cloud.effective_job_action_timeout_seconds() == 480
    assert local.scheduler_uses_fake_llm() is False
    assert cloud.scheduler_uses_fake_llm() is True
    settings = Settings(llm_runtime="local", llm_api_key=None)
    client = get_llm_client(settings)
    assert isinstance(client, OpenAICompatibleClient)
    assert not isinstance(client, StubLLMClient)


def test_cloud_without_key_still_stubs() -> None:
    settings = Settings(
        llm_runtime="cloud",
        llm_base_url="https://api.openai.com/v1",
        llm_api_key=None,
    )
    assert settings.llm_is_local() is False
    assert isinstance(get_llm_client(settings), StubLLMClient)


def test_local_planned_interval_follows_scalp_not_spend_floor() -> None:
    cloud = Settings(
        llm_runtime="cloud",
        llm_base_url="https://api.openai.com/v1",
        max_intraday_reanalyses=12,
    )
    local = Settings(
        llm_runtime="local",
        max_intraday_reanalyses=12,
        max_intraday_reanalyses_local=180,
    )
    assert planned_intraday_interval_minutes(["scalp"], cloud, session_minutes=360) == 20
    assert planned_intraday_interval_minutes(["scalp"], local, session_minutes=360) == 2
    assert effective_max_intraday_reanalyses(cloud) == 12
    assert effective_max_intraday_reanalyses(local) == 180


@pytest.mark.asyncio
async def test_local_complete_json_skips_spend_gate(tmp_path, monkeypatch) -> None:
    settings = Settings(
        llm_runtime="local",
        llm_budget_enforce=True,
        llm_daily_token_budget=10,
        llm_daily_call_budget=1,
        llm_monthly_aud_budget=0.01,
        llm_budget_state_path=str(tmp_path / "budget.json"),
        llm_api_key=SecretStr("unused"),
        llm_json_object_response=True,
    )
    record_llm_usage(prompt_tokens=100, completion_tokens=0, settings=settings)
    client = OpenAICompatibleClient(settings)

    class _Resp:
        status_code = 200

        def json(self) -> dict:
            return {
                "model": "qwen2.5:14b",
                "choices": [{"message": {"content": "{\"ok\": true}"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            }

    class _Http:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> _Http:
            return self

        async def __aexit__(self, *args) -> None:
            return None

        posted: dict = {}

        async def post(self, *args, **kwargs):
            _Http.posted = kwargs.get("json") or {}
            return _Resp()

    monkeypatch.setattr("app.services.llm.httpx.AsyncClient", _Http)
    out = await client.complete_json(system_prompt="s", user_prompt="u")
    assert out.model == "qwen2.5:14b"
    assert "ok" in out.content
    assert _Http.posted.get("num_ctx") == 32768
    assert (_Http.posted.get("options") or {}).get("num_ctx") == 32768
