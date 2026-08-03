# Alpaca Paper Adapter

## Choice: REST (httpx), not official SDK

Phase 5 uses direct REST calls via `httpx` against Alpaca Trading API v2.

Reasons:

- Pinning and mocking HTTP is simpler in unit tests than SDK object graphs
- Explicit control over paper base URL checks and credential redaction
- Avoids SDK version drift coupling order semantics to a third-party release train

If an official SDK is adopted later, keep the same `BrokerClient` / canonical models.

## Paper-only gates

On adapter construction and every request:

- `ENABLE_LIVE_TRADING` must be false
- `BROKER_ENVIRONMENT` must be `paper`
- `ALPACA_BASE_URL` must contain `paper-api`
- Dual-gate live execution mode must not resolve to live

Live URL `https://api.alpaca.markets` is refused.

## Credentials

Env only: `ALPACA_API_KEY`, `ALPACA_API_SECRET` / `ALPACA_SECRET_KEY`, `ALPACA_PAPER_BASE_URL`. Never logged in full; error bodies are redacted.

## Smoke tests

Opt-in only:

```bash
RUN_ALPACA_PAPER_SMOKE_TESTS=true pytest tests/unit/test_phase5_execution.py::test_alpaca_paper_smoke_opt_in
```

Default CI/unit runs do **not** submit paper orders.
