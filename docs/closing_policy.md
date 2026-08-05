# Closing Policy

`ClosingService` applies `ClosingPolicyEngine` to live `position_lifecycles`.

## Unattended path

Scheduler job `closing_window` → `DailyWorkflowService.start_closing` → `ClosingService.run_closing`.
Intraday interval jobs inside the force-close window also call `ClosingService` (analysis stays paused).

## Behavior

- Scalp/day watchlist horizons force flatten near close (intraday-only), even if `overnight_allowed` was mis-set.
- Creates `OrderIntent` exit rows when the intraday mode allows intents (`MANUAL_APPROVAL` / `PAPER_AUTOMATED`).
- Auto paper submit only when **all** of these hold:
  - `AUTO_EXECUTE_FORCE_CLOSE=true`
  - `ENABLE_BROKER_ORDERS=true`
  - `ENABLE_AUTOMATED_EXECUTION=true`
  - `REQUIRE_MANUAL_ORDER_APPROVAL=false`
  - Mode can submit (not observe-only)
- Default remains fail-closed: intents pending, no broker orders.
