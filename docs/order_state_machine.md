# Order State Machine

Internal states and legal transitions live in `app/brokers/models.py` (`InternalOrderState`, `ALLOWED_ORDER_TRANSITIONS`, `assert_order_transition`).

Illegal transitions raise. Broker timeout does **not** mean failed: status becomes `UNKNOWN` / `RECONCILIATION_REQUIRED` and recovery uses `client_order_id` lookup.
