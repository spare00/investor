# Order Intents

CIO JSON is never a raw broker order. After the 6-agent bottom-up pipeline, the
**CIO decision** is the trading judgment. `ExecutionService.build_intents_from_decision`
(via `firm_execution.materialize_cio_decision`) converts validated CIO symbol actions
into persisted `order_intents`.

```
Market Intelligence → Macro∥Quant → Risk → Devil → CIO
        → Hard Veto / Execution Validator
        → Order Intents
        → Paper submit (when ENABLE_BROKER_ORDERS + automated unlocked)
```

Intent types include `OPEN_LONG`, `CLOSE_LONG`, … Short selling is disabled by default.

Statuses: CREATED → VALIDATING → PENDING_APPROVAL (optional brake) / RISK_REJECTED → APPROVED → SUBMITTING → SUBMITTED.

Operator CLI/API (`approve` / `submit`) exists for the optional manual brake and recovery —
not because humans are the default traders.

CLI: `python -m app.cli execution intents list`, `build-intents`, `validate`, `approve`, `submit`.
