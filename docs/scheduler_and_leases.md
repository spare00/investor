# Scheduler and Leases

## Scheduler

- Default: `ENABLE_SCHEDULER=false` (also `SCHEDULER_ENABLED=false`).
- When enabled, APScheduler runs:
  1. Interval poller `daily_workflow_dispatch` that:
     - Checks emergency stop
     - Acquires a dispatch lease (`scheduler:dispatch`)
     - Ensures sessions are prepared and runs due `scheduled_jobs`
     - Releases the dispatch lease
     - Runs session catch-up under a separate lease (`scheduler:catch_up`) so long LLM catch-up does not block due-job ticks
  2. Interval job `universe_refresh` (when `UNIVERSE_MODE=dynamic` and `UNIVERSE_MANAGER_ENABLED=true`) under lease `scheduler:universe_refresh`. Poll cadence: `UNIVERSE_REFRESH_SECONDS` (default 6h). **LLM** runs at most every `UNIVERSE_REFRESH_MIN_INTERVAL_DAYS` (default 7) and, by default, only on operator-timezone weekends (`UNIVERSE_REFRESH_WEEKEND_ONLY=true`) so weekly pool work avoids weekday trading tokens. When weekend-only is off and `UNIVERSE_REFRESH_SESSION_ONLY=true`, ticks outside premarket→after-hours are skipped.
  3. Interval job `broker_reconciliation` (when `ENABLE_BROKER_CONNECTION` or `ENABLE_BROKER_ORDERS`) under lease `scheduler:broker_recon`. Cadence: `BROKER_RECONCILIATION_INTERVAL_SECONDS` (default 60).
- Due-job dispatch under `scheduler:dispatch` does **not** call LLM/Risk/Broker directly. Session catch-up (after that lease is released) may run analysis under venue-scoped leases; universe refresh may call the Universe Manager agent.

Each due job is wrapped in `wait_for` using `effective_job_action_timeout_seconds()` — **480s for both local and cloud**. Local 14B must finish inside that cap (compact briefs, Quant/Risk skip chat). Timeouts increment `investor_scheduler_job_timeouts_total` and show on dashboard `committee_watch`. Do not treat a longer cap as the first response to a growing watchlist.

Job plans are created in `DailyWorkflowService.prepare` from Calendar Service session times. Job keys are **venue-scoped** (`US:premarket_analysis`, `AU:intraday_eval_0`, …) so the same calendar date can hold both ASX and US books. Default `ENABLED_VENUES=US,AU` prepares/dispatches both (non-overlapping BNE wall-clock). Set `ENABLED_VENUES=US` (or `AU`) to run a single book. `intraday_eval_*` spacing follows the tightest active-watchlist horizon (scalp ≈ 2m, day ≈ 5m, …), floored so planned ticks stay near `1.5 × MAX_INTRADAY_REANALYSES` for the session; overdue intraday jobs are coalesced **per venue** to the latest. Universe refresh (scheduler or `POST /universe/refresh`) **replans** pending `intraday_eval_*` rows so cadence tracks horizon changes. Runtime gates (`min_gap`, agent cooldowns, `MAX_INTRADAY_REANALYSES`) still skip or cap early / excess runs.

### 24h BNE wall-clock (dual book)

Sessions do not overlap in Australia/Brisbane:

| BNE window (approx) | Book | What runs |
|---------------------|------|-----------|
| ~07:00–16:10 | **AU** | ASX premarket → RTH → close / postmarket |
| ~16:10–18:00+ | idle | No market session jobs due (dispatch still polls) |
| US overnight (BNE night/early morning) | **US** | NYSE premarket → RTH → close / postmarket |
| After US AH → next ASX pre-open | idle | Same |

One process, one IBKR account, two `DailyWorkflowRun`s (calendar `ASX` vs `NYSE`). Agents use the same six roles; each invocation gets a **BOOK CONTEXT** (venue, currency, allowlist, benchmark). Analysis leases are venue-scoped (`daily:US:…:analysis` / `daily:AU:…:analysis`). Manual HTTP: `GET /workflow/daily/current?venue=AU`.

## Leases

Table `workflow_leases` with unique `lease_key`.

- Acquire fails if another owner holds a non-expired lease
- Expired leases can be taken over or reclaimed
- Heartbeat extends `expires_at`
- Release requires matching owner

Used for prepare, analysis, scheduler dispatch/job keys, and `scheduler:catch_up`.
