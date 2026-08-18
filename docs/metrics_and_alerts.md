# Metrics and Alerts

## Prometheus

`GET /metrics` — workflow, agent, provider, order, drawdown, LLM budget, and committee wall-time series (see `app/core/metrics.py`).

### Committee / scheduler (local 14B vs 8-minute cap)

As the managed book grows, scrape these before timeouts become routine. Dashboard JSON `committee_watch` is the same snapshot without Prometheus.

| Metric | Type | Meaning |
|--------|------|---------|
| `investor_scheduler_job_duration_seconds{kind}` | Histogram | Wall time per scheduler job (`intraday_eval`, `premarket`, `catch_up`, …). Buckets go through 480s. |
| `investor_scheduler_job_timeouts_total{kind}` | Counter | Jobs killed by `job_action_timeout` (including catch-up). |
| `investor_last_committee_seconds` | Gauge | Wall seconds of the last finished `intraday_eval`. |
| `investor_committee_timeout_cap_seconds` | Gauge | Scheduler `wait_for` cap (480). |
| `investor_committee_headroom_ratio` | Gauge | `1 - last_eval / cap`. `0` means the cap was hit. |
| `investor_watchlist_symbols` | Gauge | Watchlist row count (grows with managed names). |
| `investor_focus_symbols` | Gauge | Focus-set size fed into collection/eval. |

Useful checks:

```promql
investor_last_committee_seconds / investor_committee_timeout_cap_seconds
rate(investor_scheduler_job_timeouts_total[1d])
```

There is no PagerDuty route for these yet — Overview pills go `warn` at 70% of cap and `bad` at 90% or any timeout this session.

## Operational KPIs

`GET /operations/metrics` aggregates job/workflow/alert rates from counters. Phase 7 returns **placeholder zeros** until Prometheus → DB bridge exists.

## Alerts

`AlertService` — dedup, cooldown, in-memory lifecycle + optional `operational_alerts` persistence.

| Endpoint | Action |
|----------|--------|
| `GET /operations/alerts` | List |
| `GET /operations/alerts/{id}` | Detail |
| `POST /operations/alerts/{id}/acknowledge` | Ack |
| `POST /operations/alerts/{id}/resolve` | Resolve |

Providers: `log` (default), `fake`, `email`, `webhook`.

## Wired emitters

| Code | When |
|------|------|
| `trading.emergency_stop` | Emergency stop API / ops / intraday recovery restore |
| `trading.hard_stop` | Hard stop exit intent created (dedupe per symbol/day) |
| `trading.monitor_emergency` | Monitor verdict `EMERGENCY_ACTION_REQUIRED` |
| `trading.overnight_review` | Overnight review flags leftovers / manual review |
| `recon.material_drift` / `recon.broker_unavailable` / `recon.local_state_invalid` | Scheduled recon, recovery recon |
| `llm.budget_exhausted` | Billable LLM call blocked by budget |
| `llm.budget_soft_limit` | Soft % of daily/monthly budget hit |

Clearing emergency stop auto-resolves open `trading.emergency_stop` alerts. Overview Ack/Resolve updates `operational_alerts` via `/operations/alerts/{id}/acknowledge|resolve`.

Helpers live in `app/alerts/ops.py` (dedupe + cooldown via `AlertService`).

## Not supported

- PagerDuty/Opsgenie native integration
- Alert routing by on-call schedule
- Auto-resolve on metric recovery
