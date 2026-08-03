# Order Idempotency

`client_order_id` / `orders.idempotency_key` is derived from workflow, decision, intent, symbol, side, attempt (`make_client_order_id`).

Guarantees:

- DB unique constraint on `orders.idempotency_key` and `order_intents.client_order_id`
- Resubmit returns the existing local row
- After timeout, broker lookup by client order id before a second submit

Not implemented as an in-memory-only cache.
