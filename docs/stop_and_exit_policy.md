# Stop and Exit Policy

Supports FIXED_PRICE, PERCENTAGE, ATR_BASED, TIME_BASED stops; multi-target take-profit with partial exits. Stop widening blocked by default; tightening allowed.

## Hard stops (unattended)

`IntradayService.monitor_all` creates Order Intents on hard stop. For unattended paper, set `AUTO_EXECUTE_HARD_STOPS=true` so exits submit immediately (with the other paper automation gates). Leaving it false is fail-closed / manual only.

When alerts are enabled, each hard-stop intent emits `trading.hard_stop` (CRITICAL, deduped per symbol/day). Monitor `EMERGENCY_ACTION_REQUIRED` emits `trading.monitor_emergency`.

Paper auto-submit only when **all** hold:

- `AUTO_EXECUTE_HARD_STOPS=true`
- `ENABLE_BROKER_ORDERS=true`
- `ENABLE_AUTOMATED_EXECUTION=true`
- `REQUIRE_MANUAL_ORDER_APPROVAL=false`
- Mode can submit (`PAPER_AUTOMATED`)

Duplicate ticks skip when lifecycle is already `PENDING_CLOSE`.

Invalidation states: NOT_TRIGGERED / POSSIBLE / CONFIRMED / UNKNOWN_DUE_TO_DATA.
