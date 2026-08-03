# Order Intents

CIO JSON is never a broker order. `ExecutionService.build_intents_from_decision` converts validated CIO symbol actions into persisted `order_intents` rows.

Intent types include `OPEN_LONG`, `CLOSE_LONG`, … Short selling is disabled by default (`ENABLE_SHORT_SELLING=false`).

Statuses move through CREATED → VALIDATING → PENDING_APPROVAL / RISK_REJECTED → APPROVED → SUBMITTING → SUBMITTED.

API: `POST /execution/intents/build`, `GET /execution/intents`, approve/reject/submit endpoints.
CLI: `python -m app.cli execution intents list`, `build-intents`, `validate`, `approve`, `submit`.
