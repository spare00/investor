# Scheduler and Leases

## Scheduler

- Default: `ENABLE_SCHEDULER=false` (also `SCHEDULER_ENABLED=false`).
- When enabled, APScheduler runs:
  1. Interval poller `daily_workflow_dispatch` that:
     - Checks emergency stop
     - Acquires a dispatch lease
     - Loads due `scheduled_jobs` rows
     - Per job: lease → `DailyWorkflowService` method → mark completed
  2. Interval job `universe_refresh` (when `UNIVERSE_MODE=dynamic` and `UNIVERSE_MANAGER_ENABLED=true`) that refreshes watchlist/focus via Universe Manager under lease `scheduler:universe_refresh`. Cadence: `UNIVERSE_REFRESH_SECONDS` (default 900).
- Workflow dispatch **does not** call LLM, Risk, or Broker directly; universe refresh may call the Universe Manager agent.

Job plans are created in `DailyWorkflowService.prepare` from Calendar Service session times.

## Leases

Table `workflow_leases` with unique `lease_key`.

- Acquire fails if another owner holds a non-expired lease
- Expired leases can be taken over or reclaimed
- Heartbeat extends `expires_at`
- Release requires matching owner

Used for prepare, analysis, and scheduler dispatch/job keys.
