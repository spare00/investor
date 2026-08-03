# Phase 3 Audit (pre Phase 4)

Date: 2026-08-04  
Command: `pytest tests/ -q` → **122 passed**

## Implemented Correctly

- `MarketCalendarService` (`exchange_calendars` XNYS) used by daily workflow prepare/job planning and market status API
- Holidays and early closes handled (e.g. Thanksgiving, day-after early close)
- Market session math via `America/New_York` `zoneinfo` (DST spring/fall covered by tests)
- Brisbane labels derived from Calendar Service `astimezone(Australia/Brisbane)`
- `DailyWorkflowRun` + `WorkflowStateTransition` persisted; illegal transitions blocked
- Unique `(session_date, calendar_name)` prevents duplicate daily runs
- `workflow_leases` unique `lease_key`; reclaim on expiry; recovery on startup
- Scheduler dispatches only to `DailyWorkflowService` (no direct LLM/broker)
- Premarket analysis calls Phase 2 `AgentPipeline` 6-agent chain
- Preopen revalidation + intraday reevaluation policy + closing policy interface present
- Pause / emergency-stop restored from `configuration_history` across restarts
- Defaults: `ENABLE_BROKER_ORDERS=false`, `ENABLE_AUTOMATED_EXECUTION=false`, `ENABLE_SCHEDULER=false`
- Phase 1–3 tests: **122 passed**

## Partially Implemented

- Lease heartbeat not called during long `run_analysis` (TTL risk under concurrency)
- Premarket/postmarket extended-hours windows use fixed 04:00 / 20:00 ET assumptions (not exchange-authoritative on early-close days)
- `_broker_guard()` in daily SM only logs if broker flags true; safety relies on never calling OrderManager
- Legacy `SCHEDULER_ENABLED` / cron fields coexist with `ENABLE_SCHEDULER` (dispatch uses only the latter)

## Missing (for Phase 4)

- Real provider adapter layer with circuit breaker / provenance / conflict models
- Wired Alpaca market data (scaffold returns empty / falls back to stub)
- SEC / news / macro beyond stubs
- Context builders with untrusted-data wrappers
- High-importance market event generation tied to intraday triggers

## Incorrect or Risky

- **Legacy `/workflow/premarket/run`** (`WorkflowService`) can call `OrderManager.submit_validated_intents` when `trading_mode=paper` without checking `ENABLE_BROKER_ORDERS` — bypasses Phase 3 daily SM safety story
- Dual workflow paths (Phase 2 decision workflow vs Phase 3 daily SM) can confuse operators

## Data Integration Risks

- `DataCollectionService` + stub collectors feed agents today; switching to live providers without quality/freshness gates risks silent bad analysis
- Earnings/SEC filings not first-class persisted tables (dict passthrough in bundle)
- `redis` unused — no shared rate-limit/cache yet

## Compatibility Concerns

- Agents consume `CollectionBundle` / normalized DTOs; Phase 4 must keep that contract or adapt via Context Builders
- Existing Alpaca/dashboard code remains; must stay outside scheduler + daily SM auto path

## Tests Executed

```bash
pytest tests/ -q
# 122 passed, 1 warning
```

## Corrections Applied

- Gate legacy `WorkflowService` order submission on `settings.enable_broker_orders` (default false) so Phase 4 data work cannot accidentally submit via old API
