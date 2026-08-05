"""Daily + monthly LLM budget hard-stop tests."""

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
        llm_monthly_aud_budget=100,
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
        llm_monthly_aud_budget=100,
        llm_budget_state_path=str(tmp_path / "budget.json"),
        llm_api_key=SecretStr("sk-test"),
    )
    record_llm_usage(prompt_tokens=60, completion_tokens=50, settings=settings)
    snap = snapshot_llm_budget(settings)
    assert snap.total_tokens == 110
    assert snap.blocked is True


def test_monthly_aud_budget_blocks(tmp_path) -> None:
    settings = Settings(
        llm_budget_enforce=True,
        llm_daily_token_budget=50_000_000,
        llm_daily_call_budget=50_000,
        llm_monthly_aud_budget=10.0,
        llm_aud_per_usd=1.55,
        llm_input_usd_per_mtok=0.15,
        llm_output_usd_per_mtok=0.60,
        llm_budget_state_path=str(tmp_path / "budget.json"),
        llm_api_key=SecretStr("sk-test"),
    )
    # ~A$10.07 at configured rates: mostly completion tokens.
    # cost = (0*0.15 + 10.85e6/1e6*0.60) * 1.55 ≈ 10.089
    record_llm_usage(prompt_tokens=0, completion_tokens=10_850_000, settings=settings)
    snap = snapshot_llm_budget(settings)
    assert snap.month_aud_estimate >= 10.0
    assert snap.month_blocked is True
    assert snap.blocked is True
    with pytest.raises(Exception, match="monthly_aud_budget"):
        assert_llm_budget_allows_call(settings)


def test_budget_disabled_allows_overage(tmp_path) -> None:
    settings = Settings(
        llm_budget_enforce=False,
        llm_daily_token_budget=1,
        llm_daily_call_budget=1,
        llm_monthly_aud_budget=0.01,
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
        llm_monthly_aud_budget=100,
        llm_budget_state_path=str(tmp_path / "budget.json"),
        llm_api_key=SecretStr("sk-test"),
    )
    record_llm_usage(prompt_tokens=20, completion_tokens=0, settings=settings)
    client = OpenAICompatibleClient(settings)
    with pytest.raises(LLMError, match="token_budget"):
        await client.complete_json(system_prompt="s", user_prompt="u")


def test_legacy_state_file_seeds_month_and_survives_reload(tmp_path) -> None:
    from datetime import UTC, datetime

    day = datetime.now(UTC).date().isoformat()
    path = tmp_path / "budget.json"
    path.write_text(
        '{"day": "%s", "prompt_tokens": 100, "completion_tokens": 20, "calls": 3, '
        '"soft_warned": false, "updated_at": "%sT00:00:00+00:00"}' % (day, day)
    )
    settings = Settings(
        llm_budget_enforce=True,
        llm_daily_token_budget=1_000_000,
        llm_daily_call_budget=1000,
        llm_monthly_aud_budget=10,
        llm_budget_state_path=str(path),
    )
    snap = snapshot_llm_budget(settings)
    assert snap.total_tokens == 120
    assert snap.calls == 3
    assert snap.month_total_tokens == 120
    assert snap.month_calls == 3

    reset_llm_budget_for_tests()
    snap2 = snapshot_llm_budget(settings)
    assert snap2.total_tokens == 120
    assert snap2.month_calls == 3
