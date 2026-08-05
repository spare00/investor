# Stop and Exit Policy

Supports FIXED_PRICE, PERCENTAGE, ATR_BASED, TIME_BASED stops; multi-target take-profit with partial exits. Stop widening blocked by default; tightening allowed.

## Hard stops (unattended)

`IntradayService.monitor_all` creates Order Intents on hard stop. Defaults stay fail-closed (`AUTO_EXECUTE_HARD_STOPS=false` → `PENDING_APPROVAL`).

Paper auto-submit only when **all** hold:

- `AUTO_EXECUTE_HARD_STOPS=true`
- `ENABLE_BROKER_ORDERS=true`
- `ENABLE_AUTOMATED_EXECUTION=true`
- `REQUIRE_MANUAL_ORDER_APPROVAL=false`
- Mode can submit (`PAPER_AUTOMATED`)

Duplicate ticks skip when lifecycle is already `PENDING_CLOSE`.

Invalidation states: NOT_TRIGGERED / POSSIBLE / CONFIRMED / UNKNOWN_DUE_TO_DATA.
