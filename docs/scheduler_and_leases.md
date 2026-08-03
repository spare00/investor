# Scheduler and Leases

## Scheduler

- Default: `ENABLE_SCHEDULER=false` (also `SCHEDULER_ENABLED=false`).
- When enabled, APScheduler runs an interval poller (`daily_workflow_dispatch`) that:
  1. Checks emergency stop
  2. Acquires a dispatch lease
  3. Loads due `scheduled_jobs` rows
  4. Per job: lease → `DailyWorkflowService` method → mark completed
- Scheduler **does not** call LLM, Risk, or Broker directly.

Job plans are created in `DailyWorkflowService.prepare` from Calendar Service session times.

## Leases

Table `workflow_leases` with unique `lease_key`.

- Acquire fails if another owner holds a non-expired lease
- Expired leases can be taken over or reclaimed
- Heartbeat extends `expires_at`
- Release requires matching owner

Used for prepare, analysis, and scheduler dispatch/job keys.
