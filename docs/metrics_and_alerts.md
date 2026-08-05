# Metrics and Alerts

## Prometheus

`GET /metrics` — workflow, agent, provider, order, drawdown gauges (see `app/core/metrics.py`).

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
| `recon.material_drift` / `recon.broker_unavailable` / `recon.local_state_invalid` | Scheduled recon, recovery recon |
| `llm.budget_exhausted` | Billable LLM call blocked by budget |
| `llm.budget_soft_limit` | Soft % of daily/monthly budget hit |

Helpers live in `app/alerts/ops.py` (dedupe + cooldown via `AlertService`).

## Not supported

- PagerDuty/Opsgenie native integration
- Alert routing by on-call schedule
- Auto-resolve on metric recovery
