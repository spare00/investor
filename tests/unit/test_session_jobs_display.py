"""Session job display enrichment for dashboard."""

from __future__ import annotations

from app.api.dashboard import enrich_session_jobs


def test_enrich_session_jobs_intraday_seq_by_planned_time() -> None:
    rows = enrich_session_jobs(
        [
            {
                "job_key": "AU:intraday_eval_5",
                "venue": "AU",
                "planned_at": "2026-08-12T02:00:00+00:00",
                "status": "completed",
            },
            {
                "job_key": "AU:intraday_eval_0",
                "venue": "AU",
                "planned_at": "2026-08-12T00:19:00+00:00",
                "status": "completed",
            },
            {
                "job_key": "AU:intraday_eval_6",
                "venue": "AU",
                "planned_at": "2026-08-12T02:19:00+00:00",
                "status": "planned",
            },
        ]
    )
    by_key = {r["job_key"]: r for r in rows}
    assert by_key["AU:intraday_eval_0"]["intraday_seq"] == 1
    assert by_key["AU:intraday_eval_0"]["display_name"] == "Intraday eval #1"
    assert by_key["AU:intraday_eval_5"]["intraday_seq"] == 2
    assert by_key["AU:intraday_eval_6"]["intraday_seq"] == 3
    assert by_key["AU:intraday_eval_6"]["plan_index"] == 6
    assert by_key["AU:intraday_eval_0"]["duration_s"] is None


def test_enrich_session_jobs_includes_premarket_in_session_seq() -> None:
    rows = enrich_session_jobs(
        [
            {
                "job_key": "AU:premarket_analysis",
                "venue": "AU",
                "planned_at": "2026-08-11T22:00:00+00:00",
                "status": "completed",
            },
            {
                "job_key": "AU:intraday_eval_0",
                "venue": "AU",
                "planned_at": "2026-08-12T00:19:00+00:00",
                "status": "planned",
            },
        ]
    )
    pre = next(r for r in rows if r["job_key"] == "AU:premarket_analysis")
    intra = next(r for r in rows if r["job_key"] == "AU:intraday_eval_0")
    assert pre["session_seq"] == 1
    assert pre["display_name"] == "Premarket analysis"
    assert intra["session_seq"] == 2
    assert intra["intraday_seq"] == 1
    assert intra["display_name"] == "Intraday eval #1"
