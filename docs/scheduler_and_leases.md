# Scheduler and Leases

## Scheduler

- Default: `ENABLE_SCHEDULER=false` (also `SCHEDULER_ENABLED=false`).
- When enabled, APScheduler runs:
  1. Interval poller `daily_workflow_dispatch` that:
     - Checks emergency stop
     - Acquires a dispatch lease
     - Loads due `scheduled_jobs` rows
     - Per job: lease → `DailyWorkflowService` method → mark completed
  2. Interval job `universe_refresh` (when `UNIVERSE_MODE=dynamic` and `UNIVERSE_MANAGER_ENABLED=true`) under lease `scheduler:universe_refresh`. Poll cadence: `UNIVERSE_REFRESH_SECONDS` (default 7d). **LLM** runs at most every `UNIVERSE_REFRESH_MIN_INTERVAL_DAYS` (default 7); otherwise focus is rebuilt without the model. When `UNIVERSE_REFRESH_SESSION_ONLY=true` (default), ticks outside premarket→after-hours are skipped.
  3. Interval job `broker_reconciliation` (when `ENABLE_BROKER_CONNECTION` or `ENABLE_BROKER_ORDERS`) under lease `scheduler:broker_recon`. Cadence: `BROKER_RECONCILIATION_INTERVAL_SECONDS` (default 60).
- Workflow dispatch **does not** call LLM, Risk, or Broker directly; universe refresh may call the Universe Manager agent.

Job plans are created in `DailyWorkflowService.prepare` from Calendar Service session times. Job keys are **venue-scoped** (`US:premarket_analysis`, `AU:intraday_eval_0`, …) so the same calendar date can hold both ASX and US books. Set `ENABLED_VENUES=US,AU` to prepare/dispatch both; default is the primary venue only. `intraday_eval_*` spacing follows the tightest active-watchlist horizon (scalp ≈ 2m, day ≈ 5m, …), floored so planned ticks stay near `1.5 × MAX_INTRADAY_REANALYSES` for the session; overdue intraday jobs are coalesced **per venue** to the latest. Universe refresh (scheduler or `POST /universe/refresh`) **replans** pending `intraday_eval_*` rows so cadence tracks horizon changes. Runtime gates (`min_gap`, agent cooldowns, `MAX_INTRADAY_REANALYSES`) still skip or cap early / excess runs.

## Leases

Table `workflow_leases` with unique `lease_key`.

- Acquire fails if another owner holds a non-expired lease
- Expired leases can be taken over or reclaimed
- Heartbeat extends `expires_at`
- Release requires matching owner

Used for prepare, analysis, and scheduler dispatch/job keys.
