# Manual Approval (optional ops brake)

**Identity:** Trading authority belongs to the **CIO** after bottom-up 6-agent analysis.
Hard Risk Veto cannot be overridden. Humans are operators and emergency brakes —
not the default order approvers.

## Defaults (ship-safe)

| Flag | Default | Meaning |
|------|---------|---------|
| `ENABLE_BROKER_ORDERS` | `false` | No broker submit until explicitly unlocked |
| `ENABLE_AUTOMATED_EXECUTION` | `false` | Firm paper auto-submit off until unlocked |
| `REQUIRE_MANUAL_ORDER_APPROVAL` | `false` | Optional brake — **not** the firm model |
| `ENABLE_LIVE_TRADING` | `false` | Always blocked in current phases |

## Agent-firm paper path (when unlocked)

```
6-Agent pipeline → CIO Decision → ExecutionValidator + Risk Engine
        → Order Intents
        → Paper broker submit (Mock / Alpaca Paper)
```

Unlock paper automation:

```
ENABLE_BROKER_ORDERS=true
ENABLE_AUTOMATED_EXECUTION=true
REQUIRE_MANUAL_ORDER_APPROVAL=false
BROKER_ENVIRONMENT=paper
ENABLE_LIVE_TRADING=false
INTRADAY_OPERATION_MODE=PAPER_AUTOMATED
```

## Optional manual brake

When `REQUIRE_MANUAL_ORDER_APPROVAL=true`, intents stop at `PENDING_APPROVAL`
until an operator approves. This is a **safety latch**, not the product identity.

There is no free-form “place order for symbol X” API — orders must come from
Decision → Intent (CIO path).
