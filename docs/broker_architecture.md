# Broker Architecture (Phase 5)

## Overview

Investor talks to brokers through a shared adapter boundary. CIO / LLM agents never call the broker. Orders are produced only by deterministic code after Decision → Intent → Pre-trade Risk → Approval.

```
CIO Decision
  → Decision Validator
  → Order Intent Builder (ExecutionService)
  → Pre-trade Risk Validator
  → Approval Workflow (manual by default)
  → Broker Order Request
  → Broker Adapter (Mock | Alpaca Paper)
```

## Providers

| Provider | Purpose |
|----------|---------|
| `mock` (default) | Offline deterministic fills; full unit/E2E without network |
| `alpaca` | Paper trading HTTP adapter (`https://paper-api.alpaca.markets`) — legacy; see tag `alpaca-paper-final` |
| `ibkr` | Paper trading via TWS API + local IB Gateway (see `docs/ibkr_paper.md`) |

Factory: `app/brokers/factory.py`. Live environment / `ENABLE_LIVE_TRADING=true` raises immediately.

Venues (`PRIMARY_VENUE=US|AU`, `OrderRequest.venue`, `ENABLED_VENUES`): calendar/currency/IB contract hints and dual-book scheduler prepare/dispatch. Positions are unique on `(symbol, venue)`. Cross-currency new entries fail closed until FX-normalized sizing exists — see `docs/market_calendar.md` and `docs/scheduler_and_leases.md`.

## Canonical models

Pydantic models in `app/brokers/models.py` (`BrokerAccount`, `BrokerPosition`, `BrokerOrder`, …). Adapters may keep legacy `OrderRequest`/`OrderResult` for submission compatibility; API surfaces prefer canonical models with redacted account IDs.

## Safety defaults

- `BROKER_PROVIDER=mock`
- `ENABLE_BROKER_CONNECTION=false`
- `ENABLE_BROKER_ORDERS=false`
- `REQUIRE_MANUAL_ORDER_APPROVAL=true`
- `ENABLE_AUTOMATED_EXECUTION=false`
- `ENABLE_LIVE_TRADING=false`

Paper Alpaca orders require provider=alpaca, environment=paper, connection+orders enabled, live=false.

## Related docs

See `docs/alpaca_paper.md`, `docs/order_intents.md`, `docs/pretrade_risk.md`, `docs/order_state_machine.md`, `docs/order_idempotency.md`, `docs/reconciliation.md`, `docs/emergency_stop.md`, `docs/manual_approval.md`.
