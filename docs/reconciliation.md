# Reconciliation

`ReconciliationService` compares local open orders to broker open orders and records `broker_reconciliation_runs` with result:

`IN_SYNC` | `MINOR_DRIFT` | `MATERIAL_DRIFT` | `BROKER_UNAVAILABLE` | `LOCAL_STATE_INVALID`

Material drift should block new risk-taking (operator review). Broker is source of truth for positions; local history is not blindly overwritten — issues are append-only events.

Triggers: `ON_DEMAND`, `SCHEDULED` (APScheduler when `ENABLE_SCHEDULER` and broker connection/orders are on), startup/recovery hooks, CLI `execution reconcile`, `POST /execution/reconcile`.

Cadence: `BROKER_RECONCILIATION_INTERVAL_SECONDS` (default 60, min 30). Scheduled runs also soft-sync positions via `PositionManager`.
