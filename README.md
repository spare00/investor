# Investor

Six-agent AI investment firm for US equities. The system collects pre-market news,
macro and market data, runs a bottom-up agent pipeline, and executes **paper trades**
via Alpaca. The objective is **controlled drawdowns and sustainable compounding**,
not short-term return maximization.

> **Safety default:** Paper trading only. Live order routing is dual-gated and off
> by default. LLM agents never call the broker API directly.

---

## Goals

| Goal | Meaning |
|------|---------|
| Capital preservation first | Hard risk vetoes can block any CIO decision |
| Compounding over lottery tickets | Conservative position sizing and cash floors |
| Full auditability | Every decision stores inputs, prompts, models, risk checks, outcomes |
| Fail closed | Incomplete data, API errors, or schema failures → no new risk |

---

## Architecture (layers)

```
Data Collection Layer
        ↓
Normalization & Storage Layer
        ↓
Agent Analysis Layer          (MI → Macro∥Quant → Risk → Devil → CIO)
        ↓
Decision & Risk Layer         (Hard Veto + Execution Validator)
        ↓
Execution Layer               (Paper broker adapter — Phase 6)
        ↓
Monitoring & Post-Trade Review
```

### Bottom-up workflow (mandatory order)

1. Collect & normalize data  
2. Market Intelligence Analyst  
3. Macro Strategist **and** Quant Strategist (parallel)  
4. Portfolio & Risk Manager (LLM + deterministic risk engine)  
5. Devil’s Advocate  
6. CIO / Final Decision Maker  
7. Deterministic decision validation  
8. Paper order execution (Phase 6+)  
9. Intraday re-evaluation  
10. Post-market review  

Higher agents receive both raw source payloads and structured lower-agent reports.

### Six agents

| Agent | May decide trades? | Notes |
|-------|--------------------|-------|
| Market Intelligence | No | Facts vs interpretation; no trade calls |
| Macro & Policy | Regime only | Classifies RISK_ON … STRONG_RISK_OFF |
| Quant & Technical | Levels/probs | Probabilities from rules/models, not vibes |
| Portfolio & Risk | Approve/veto | Hard vetoes are **code**, not LLM |
| Devil’s Advocate | Challenge | Must answer five mandatory questions |
| CIO | Final action | **Cannot** override Hard Veto |

---

## Architecture review & supplements

Beyond the original brief, Phase 1 adopts these additions:

1. **Dual-gate live trading** — `LIVE_TRADING_ENABLED=true` *and*
   `LIVE_TRADING_CONFIRMATION_TOKEN == EXPECTED_LIVE_CONFIRMATION_TOKEN`.
   One env flag alone cannot enable live orders.
2. **`app/risk/` package** — Deterministic Risk Engine separated from LLM agent code
   so veto logic is unit-testable without network/LLM.
3. **Broker interface before Alpaca** — `brokers/base.py` adapter pattern so paper
   simulation and future brokers stay swappable (stubs only in Phase 1).
4. **Idempotency keys** on every intended order path (enforced in risk/execution
   contracts before Phase 6 ships orders).
5. **Exchange calendar** (`exchange-calendars`) for NYSE holidays and early closes.
6. **Display timezones** — storage UTC; UI/logs can render `America/New_York` and
   `Australia/Brisbane`.
7. **Prometheus `/metrics`** planned alongside FastAPI (stub wiring in later phases).
8. **News provider abstraction** with a `stub` provider for offline tests.
9. **Intraday min re-eval + cooldown** to avoid overtrading.
10. **Configuration history** table (schema in Phase 2 models) for policy audits.

Assumptions documented under [Assumptions](#assumptions).

---

## Data flow (Phase 1 view)

```
Collectors (stubs) → Normalized DTOs → PostgreSQL
                              ↓
                     Agent I/O Schemas (Pydantic)
                              ↓
                     Risk Engine (deterministic)
                              ↓
                     (Execution deferred — Phase 6)
```

Phase 1 delivers schemas, config, logging, DB session factory, and Risk Engine
interfaces/tests. No live or paper orders are placed yet.

---

## Safety principles

- Default `TRADING_MODE=paper`.
- Live trading requires dual confirmation tokens; otherwise `is_live_trading_allowed()` is false.
- Risk Hard Vetoes are implemented in Python (not prompts).
- Incomplete / stale / low-quality data → reject new risk (fail closed).
- LLM output must pass Pydantic schema validation; retry then abort safely.
- Exceptions are logged and (in later phases) written to `system_events`.
- Secrets only via environment variables; never in source.

---

## Tech stack

Python 3.12+, FastAPI, Pydantic v2, SQLAlchemy 2 (async), PostgreSQL, Redis,
APScheduler, Alpaca Paper API (Phase 6), OpenAI-compatible LLM, pytest,
Docker Compose, structured logging (structlog).

---

## Project layout

```
investor/
├── app/
│   ├── api/           # FastAPI routes (later phases)
│   ├── agents/        # Six specialist agents (Phase 3)
│   ├── brokers/       # Broker adapters
│   ├── collectors/    # News / market / macro (Phase 2)
│   ├── core/          # Config, logging, DB, security
│   ├── decision/      # Workflow orchestration (Phase 5)
│   ├── execution/     # Order/position managers (Phase 6)
│   ├── models/        # SQLAlchemy models (Phase 2)
│   ├── risk/          # Deterministic risk engine (Phase 1+)
│   ├── schemas/       # Pydantic agent & shared schemas
│   ├── services/      # Domain services
│   ├── storage/       # Repositories
│   └── main.py
├── docs/
├── prompts/
├── tests/
├── migrations/
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
└── .env.example
```

---

## Quick start (Phase 1)

```bash
# 1. Python env
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 2. Config
cp .env.example .env

# 3. Optional infra
docker compose up -d db redis

# 4. Tests (no broker / LLM required)
pytest tests/unit -q
```

Run the API skeleton:

```bash
uvicorn app.main:app --reload --port 8000
# GET http://localhost:8000/health
```

---

## Risk policy defaults

| Parameter | Default |
|-----------|---------|
| Starting cash | 25,000 |
| Max position | 10% |
| Max sector | 30% |
| Max gross exposure | 70% |
| Min cash | 30% |
| Risk per trade | 0.5% |
| Daily max loss | 1.5% |
| Max drawdown | 8% |
| Max open positions | 8 |
| Consecutive losses → cooldown | 3 / 30 min |
| Consecutive losses → halt day | 5 |
| Force flatten before close | 15 min |

Allowlist (editable via `TRADE_ALLOWLIST`):
`SPY, QQQ, IWM, DIA, NVDA, MSFT, AMZN, GOOGL, META, AVGO, AMD, AAPL, TSLA, IONQ`

---

## Assumptions

1. **Paper first:** Phase 1–5 never submit broker orders; Phase 6 uses Alpaca paper.
2. **LLM optional in unit tests:** Risk Engine and schema tests run without API keys.
3. **Stub news/market providers** used until Phase 2 adapters are wired.
4. **Sector map** for concentration checks uses a static ETF/stock sector table
   (configurable later).
5. **ATR-based sizing** uses caller-supplied ATR/stop distance; collectors will
   populate these in Phase 2+.
6. **SQLite for unit tests** via `aiosqlite` when `DATABASE_URL` is unset in pytest.
7. **macOS + Linux** supported; Windows is best-effort only.

---

## Development phases

| Phase | Focus | Status |
|-------|--------|--------|
| 1 | Foundation, schemas, Risk Engine | **Complete** |
| 2 | Data collection & normalization | Planned |
| 3 | Agent framework + prompts | Planned |
| 4 | Full risk / emergency stop | Planned |
| 5 | Premarket / intraday / postmarket workflows | Planned |
| 6 | Paper trading execution | Planned |
| 7 | Dashboard & metrics | Planned |

---

## License

Proprietary — internal use.
