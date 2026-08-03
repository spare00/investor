# Manual Approval

Default: `REQUIRE_MANUAL_ORDER_APPROVAL=true`.

Flow: validate intent → `PENDING_APPROVAL` + `order_approvals` row → operator `approve` / `reject` → only then `submit` (and only if `ENABLE_BROKER_ORDERS=true`).

Approval expiry: `ORDER_APPROVAL_EXPIRY_MINUTES` (default 10).

Automated submit additionally needs `ENABLE_AUTOMATED_EXECUTION=true` and `REQUIRE_MANUAL_ORDER_APPROVAL=false`. Phase 5 default ops keep automation off.

There is no free-form “place order for symbol X” API — orders must come from Decision → Intent.
