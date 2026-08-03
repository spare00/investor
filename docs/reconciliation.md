# Reconciliation

`ReconciliationService` compares local open orders to broker open orders and records `broker_reconciliation_runs` with result:

`IN_SYNC` | `MINOR_DRIFT` | `MATERIAL_DRIFT` | `BROKER_UNAVAILABLE` | `LOCAL_STATE_INVALID`

Material drift should block new risk-taking (operator review). Broker is source of truth for positions; local history is not blindly overwritten — issues are append-only events.

Triggers: `ON_DEMAND`, startup/recovery hooks, CLI `execution reconcile`, `POST /execution/reconcile`.
