# Closing Policy

`ClosingService` applies `ClosingPolicyEngine` to live `position_lifecycles`.

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
