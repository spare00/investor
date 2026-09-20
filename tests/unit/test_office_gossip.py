"""Office gossip: role-thoughts from the book, skip when the committee holds the GPU."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.agents.activity import mark_agent_started, reset_agent_activity_for_tests
from app.core.config import Settings
from app.office.gossip import (
    FALLBACK_LINES,
    committee_holding_gpu,
    desk_facts_from_summary,
    dialogue_beats,
    dialogue_threads,
    next_office_gossip,
    parse_gossip_lines,
    parse_gossip_thoughts,
    remember_office_desk,
    reset_office_gossip_for_tests,
    role_thoughts_from_facts,
    thought_pool_from_facts,
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


def test_parse_gossip_thoughts_keyed() -> None:
    raw = '{"thoughts":{"cio":"손실인데 SCALE_IN이라 걱정돼.","devils_advocate":"왜 또 사자고 해."}}'
    thoughts = parse_gossip_thoughts(raw)
    assert thoughts["cio"].startswith("손실")
    assert "사자고" in thoughts["devils_advocate"]


def test_parse_gossip_strips_bullets_and_rejects_urls() -> None:
    raw = "\n".join(
        [
            "1. 창가 좀 춥다.",
            "http://evil.example",
            "짧",
            "점심 뭐 먹지.",
        ]
    )
    lines = parse_gossip_lines(raw)
    assert "창가 좀 춥다." in lines
    assert "점심 뭐 먹지." in lines
    assert all("http" not in ln.lower() for ln in lines)


def test_devil_and_cio_speak_from_the_book() -> None:
    facts = desk_facts_from_summary(
        {
            "portfolio": {"daily_pnl_pct": -4.3, "cash_pct": 70, "drawdown_pct": 4.3},
            "cio": {"portfolio_action": "SCALE_IN", "market_regime": "RISK_OFF"},
            "positions": [{"symbol": "CBA", "unrealized_pnl": -1200}],
            "agents": {
                "devils_advocate": {
                    "payload": {
                        "prefer_no_trade": True,
                        "challenge_score": 0.8,
                        "recommendation": "NO_TRADE",
                    }
                },
                "quant_strategist": {
                    "payload": {
                        "market_trend_state": "SIDEWAYS",
                        "market_volatility_state": "NORMAL",
                    }
                },
            },
        }
    )
    thoughts = role_thoughts_from_facts(facts)
    assert "SCALE_IN" in thoughts["cio"]
    assert "손실" in thoughts["cio"]
    assert "devils_advocate" in thoughts
    assert "SCALE_IN" in thoughts["devils_advocate"] or "사자고" in thoughts["devils_advocate"]
    assert "횡보" in thoughts["quant_strategist"]


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
async def test_next_office_gossip_uses_desk_thoughts_in_pytest() -> None:
    remember_office_desk(
        desk_facts_from_summary(
            {
                "portfolio": {"daily_pnl_pct": -2.0, "cash_pct": 80},
                "cio": {"portfolio_action": "STAY_CASH", "market_regime": "NEUTRAL"},
            }
        )
    )
    out = await next_office_gossip(Settings(llm_runtime="local", llm_api_key=None))
    assert out["thoughts"]["cio"]
    assert "STAY_CASH" in out["thoughts"]["cio"] or "현금" in out["thoughts"]["cio"]


@pytest.mark.asyncio
async def test_fill_cache_uses_local_http_and_stores_thoughts(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.office import gossip as mod

    remember_office_desk(
        desk_facts_from_summary(
            {
                "portfolio": {"daily_pnl_pct": -1.0, "cash_pct": 60},
                "cio": {"portfolio_action": "SCALE_IN", "market_regime": "NEUTRAL"},
            }
        )
    )

    class _Resp:
        status_code = 200

        def json(self) -> dict:
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"thoughts":{"cio":"손실 중인데 왜 사나.","devils_advocate":"반대다."}}'
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
            assert json["max_tokens"] <= 180
            return _Resp()

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    settings = Settings(
        llm_runtime="local", llm_api_key=None, llm_local_fast_model="qwen2.5:7b"
    )
    await mod._fill_cache(settings)
    assert "cio" in mod._cache_thoughts
    assert "손실" in mod._cache_thoughts["cio"]


def _blocked_cba_summary() -> dict:
    return {
        "portfolio": {"daily_pnl_pct": -1.2, "cash_pct": 72, "drawdown_pct": 1.2},
        "cio": {
            "portfolio_action": "STAY_CASH",
            "market_regime": "NEUTRAL",
            "payload": {
                "symbol_actions": [{"symbol": "CBA", "action": "BUY"}],
                "reason_not_to_trade": "devil veto",
            },
        },
        "agents": {
            "devils_advocate": {
                "payload": {
                    "prefer_no_trade": True,
                    "recommendation": "NO_TRADE",
                }
            },
            "quant_strategist": {
                "payload": {
                    "market_trend_state": "SIDEWAYS",
                    "symbol_views": [
                        {"symbol": "CBA", "entry_zone": {"min": 140.0, "max": 142.0}}
                    ],
                }
            },
        },
    }


def test_personality_from_last_book_clips() -> None:
    facts = desk_facts_from_summary(_blocked_cba_summary())
    thoughts = role_thoughts_from_facts(facts)
    assert thoughts["cio"] == "CBA 승인했는데 반대해서 못 샀어."
    assert "CBA" in thoughts["market_intelligence"]
    assert "건의" in thoughts["market_intelligence"]
    assert "보류" in thoughts["devils_advocate"]


def test_dialogue_beats_cio_devil() -> None:
    facts = desk_facts_from_summary(_blocked_cba_summary())
    beats = dialogue_beats(facts)
    pair = next(b for b in beats if b["who"] == "cio" and b["reply_who"] == "devils_advocate")
    assert "CBA" in pair["line"]
    assert "CBA" in pair["reply"]
    mi = next(b for b in beats if b["who"] == "market_intelligence" and b["reply_who"] == "cio")
    assert "건의" in mi["line"]
    thread = next(t for t in dialogue_threads(facts) if t.get("tick") == "CBA")
    assert thread["topic"] == "blocked"
    assert thread["close_who"] == "devils_advocate"
    assert "보류" in thread["close"]


def test_threads_keep_loss_and_suggestion_apart() -> None:
    week = {
        "suggested": ["AMD"],
        "blocked": [],
        "bought": [],
        "winners": [],
        "losers": [{"s": "NVDA", "pnl": -120}],
        "regimes": ["RISK_ON"],
        "pnl": -120,
        "n_closes": 1,
        "n_decisions": 2,
    }
    facts = desk_facts_from_summary(
        {
            "portfolio": {"daily_pnl_pct": 0.0, "cash_pct": 80},
            "cio": {"portfolio_action": "STAY_CASH", "market_regime": "NEUTRAL"},
            "agents": {
                "quant_strategist": {
                    "payload": {
                        "symbol_views": [{"symbol": "AMD", "entry_zone": {"min": 1, "max": 2}}]
                    }
                }
            },
        },
        week=week,
    )
    threads = dialogue_threads(facts)
    loss = next(t for t in threads if t["topic"] == "loss")
    sug = next(t for t in threads if t["topic"] == "suggested")
    assert "NVDA" in loss["line"]
    assert "손절" in loss["reply"]
    assert "건의" not in loss["line"] and "건의" not in loss["reply"]
    assert "AMD" in sug["line"]
    assert "손절" not in sug["reply"]
    assert sug.get("close_who") == "quant_strategist"


@pytest.mark.asyncio
async def test_next_office_gossip_returns_beats() -> None:
    remember_office_desk(desk_facts_from_summary(_blocked_cba_summary()))
    out = await next_office_gossip(Settings(llm_runtime="local", llm_api_key=None))
    assert out["beats"]
    assert out["thoughts"]["cio"].startswith("CBA")
    assert any(b["who"] == "cio" for b in out["beats"])


def test_llm_overlay_does_not_drop_ticker_lines() -> None:
    from app.office import gossip as mod

    remember_office_desk(desk_facts_from_summary(_blocked_cba_summary()))
    merged = mod._merged_thoughts(
        {"cio": "커피 또 내렸어.", "devils_advocate": "BHP는 아니지. 보류가 맞아."}
    )
    assert "CBA" in merged["cio"]
    assert "BHP" in merged["devils_advocate"]


def test_week_review_covers_several_names() -> None:
    week = {
        "suggested": ["AMD", "IONQ", "CBA"],
        "blocked": ["CBA"],
        "bought": ["IONQ"],
        "sold": [],
        "winners": [{"s": "AMD", "pnl": 40}],
        "losers": [{"s": "NVDA", "pnl": -120}],
        "regimes": ["RISK_ON"],
        "pnl": -80,
        "n_closes": 4,
        "n_decisions": 6,
    }
    facts = desk_facts_from_summary(_blocked_cba_summary(), week=week)
    blob = " ".join(ln for lines in thought_pool_from_facts(facts).values() for ln in lines)
    assert "AMD" in blob
    assert "IONQ" in blob
    assert "CBA" in blob
    assert "NVDA" in blob
    beats = dialogue_beats(facts)
    joined = " ".join(f"{b['line']} {b['reply']}" for b in beats)
    assert "CBA" in joined
    assert "AMD" in joined or "IONQ" in joined or "NVDA" in joined
    assert len(beats) >= 3


def test_week_review_from_records_splits_blocked_and_closes() -> None:
    from types import SimpleNamespace

    from app.office.week import week_review_from_records

    week = week_review_from_records(
        decisions=[
            SimpleNamespace(
                portfolio_action="STAY_CASH",
                reason_not_to_trade="devil veto",
                risk_approval=False,
                market_regime="RISK_ON",
                payload={
                    "symbol_actions": [
                        {"symbol": "CBA", "action": "BUY"},
                        {"symbol": "AMD", "action": "BUY"},
                    ]
                },
            ),
            SimpleNamespace(
                portfolio_action="SCALE_IN",
                reason_not_to_trade=None,
                risk_approval=True,
                market_regime="RISK_ON",
                payload={"symbol_actions": [{"symbol": "IONQ", "action": "BUY"}]},
            ),
        ],
        closes=[
            SimpleNamespace(symbol="NVDA", realized_pl=-120.0, unrealized_pl=0.0),
            SimpleNamespace(symbol="AMD", realized_pl=40.0, unrealized_pl=0.0),
            SimpleNamespace(symbol="AMD", realized_pl=-10.0, unrealized_pl=0.0),
        ],
    )
    assert "CBA" in week["blocked"]
    assert "AMD" in week["blocked"]
    assert "IONQ" in week["bought"]
    assert week["losers"][0]["s"] == "NVDA"
    assert week["winners"][0]["s"] == "AMD"
    assert week["winners"][0]["pnl"] == 30.0
