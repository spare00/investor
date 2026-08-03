"""News dedupe utility + freshness helpers (Phase 1 stubs used by collectors later)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.data_quality import (
    dedupe_news_headlines,
    is_fresh,
    score_freshness,
)


def test_dedupe_news_headlines() -> None:
    items = [
        {"headline": "Fed Holds Rates Steady", "source": "Reuters"},
        {"headline": "fed holds rates steady", "source": "Bloomberg"},
        {"headline": "CPI Comes In Hot", "source": "WSJ"},
    ]
    unique = dedupe_news_headlines(items)
    assert len(unique) == 2


def test_freshness_score_and_gate() -> None:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    fresh_ts = now - timedelta(minutes=10)
    stale_ts = now - timedelta(hours=6)
    assert is_fresh(fresh_ts, now=now, max_age_minutes=60) is True
    assert is_fresh(stale_ts, now=now, max_age_minutes=60) is False
    assert score_freshness(fresh_ts, now=now, max_age_minutes=60) > 0.8
    assert score_freshness(stale_ts, now=now, max_age_minutes=60) < 0.2
