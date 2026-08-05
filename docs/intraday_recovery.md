# Intraday Recovery

Order: restore emergency controls → broker reconciliation → poll order updates → sync `PositionLifecycle` from broker positions → inventory open lifecycles / unknown orders / pending events.

New orders remain blocked until reconciliation is clean and emergency is cleared.

## Triggers

- API startup (best-effort) when `ENABLE_BROKER_CONNECTION` or `ENABLE_BROKER_ORDERS` is on — after workflow `RecoveryService`
- `POST /intraday/recovery` / CLI `intraday recovery run`

## Alerts

When alerts are enabled, recovery emits:

- `trading.emergency_stop` (CRITICAL) if emergency is active / restored
- `recon.material_drift` / `recon.broker_unavailable` / `recon.local_state_invalid` on bad recon results
