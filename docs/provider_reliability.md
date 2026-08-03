# Provider Reliability

## Scope

Track data provider uptime, latency, failure rates, and incident impact.

## Tables

- `provider_reliability_metrics`
- `provider_incidents`

## API / CLI

- `GET /performance/providers`
- `python -m app.cli providers reliability`

## Current state

Provider list + breaker health; Prometheus counters (`investor_provider_*`) exist but **in-process operational metrics use placeholder counters** until scrape bridge ships.

## Not supported

- Automatic pager escalation on provider SLA breach
- Historical incident replay UI
- Paid vendor SLA dashboards
