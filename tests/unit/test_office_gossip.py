"""Office gossip: local-only, skip when the committee holds the GPU."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.agents.activity import mark_agent_started, reset_agent_activity_for_tests
from app.core.config import Settings
from app.office.gossip import (
    FALLBACK_LINES,
    committee_holding_gpu,
    next_office_gossip,
    parse_gossip_lines,
    reset_office_gossip_for_tests,
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_agent_activity_for_tests()
    reset_office_gossip_for_tests()
    yield
    reset_agent_activity_for_tests()
    reset_office_gossip_for_tests()


def test_parse_gossip_json_blob() -> None:
    raw = 'Sure.\n{"lines":["커피 또 내렸어.","게시판 그대로네.","who left a mug"]}\n'
    lines = parse_gossip_lines(raw)
    assert "커피 또 내렸어." in lines
    assert "게시판 그대로네." in lines
    assert "who left a mug" in lines


def test_parse_gossip_strips_bullets_and_rejects_advice() -> None:
    raw = "\n".join(
        [
            "1. 창가 좀 춥다.",
            "- buy AAPL now",
            "http://evil.example",
            "짧",
            "점심 뭐 먹지.",
        ]
    )
    lines = parse_gossip_lines(raw)
    assert "창가 좀 춥다." in lines
    assert "점심 뭐 먹지." in lines
    assert all("buy" not in ln.lower() for ln in lines)
    assert all("http" not in ln.lower() for ln in lines)


def test_committee_holding_gpu_while_running() -> None:
    assert committee_holding_gpu() is False
    mark_agent_started("market_intelligence", run_id="wf-1")
    assert committee_holding_gpu() is True


def test_committee_holding_gpu_grace_after_finish(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.office import gossip as mod

    now = datetime(2026, 9, 18, 1, 0, tzinfo=UTC)
    monkeypatch.setattr(
        "app.office.gossip.snapshot_agent_activity",
        lambda: {
            "cio": {
                "state": "idle",
                "finished_at": (now - timedelta(seconds=10)).isoformat(),
            }
        },
    )
    assert committee_holding_gpu(now) is True
    assert committee_holding_gpu(now + timedelta(seconds=mod._COMMITTEE_GRACE_SECONDS + 1)) is False


@pytest.mark.asyncio
async def test_next_office_gossip_skips_when_committee_running() -> None:
    mark_agent_started("cio")
    out = await next_office_gossip(Settings(llm_runtime="local", llm_api_key=None))
    assert out["source"] == "skipped"
    assert out["reason"] == "committee_busy"
    assert out["lines"]


@pytest.mark.asyncio
async def test_next_office_gossip_fallback_in_pytest() -> None:
    out = await next_office_gossip(Settings(llm_runtime="local", llm_api_key=None))
    assert out["source"] == "fallback"
    assert out["reason"] == "test"
    assert out["lines"][0] in FALLBACK_LINES


@pytest.mark.asyncio
async def test_fill_cache_uses_local_http_and_stores_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.office import gossip as mod

    class _Resp:
        status_code = 200

        def json(self) -> dict:
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"lines":["커피 한 잔 더.","복도 조용하다."]}'
                        }
                    }
                ]
            }

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None):
            assert "/chat/completions" in url
            assert json["max_tokens"] <= 80
            return _Resp()

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    settings = Settings(
        llm_runtime="local", llm_api_key=None, llm_local_fast_model="qwen2.5:7b"
    )
    await mod._fill_cache(settings)
    assert "커피 한 잔 더." in mod._cache_lines
    assert "복도 조용하다." in mod._cache_lines
