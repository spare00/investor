# Recovery

`RecoveryService` runs on API startup (best-effort) and via `POST /operations/recovery` / `python -m app.cli recovery run`.

## Actions

1. Restore pause / emergency-stop from `configuration_history` key `ops.trading_controls` (survives process restart).
2. Reclaim expired leases.
3. Scan incomplete `daily_workflow_runs`:
   - Prior session still running → mark `FAILED` (no late orders)
   - Premarket missed after open → annotate `NO_TRADE` default note
   - Closing missed after session end → annotate; **no broker**
   - Postmarket eligible → flag for operator/CLI resume
4. Emergency stop is **not** cleared by restart.

## Policy sketch

| Missed work | Condition | Policy |
|-------------|-----------|--------|
| Premarket analysis | Still pre-open | Eligible to run |
| Premarket analysis | Already regular hours | Prefer NO_TRADE / limited path |
| Closing window | After close | Record only; no orders |
| Postmarket review | Next start | Eligible |
