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
Execution Layer               (Mock / Alpaca Paper — Phase 5)
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
| CIO | **Yes — final trades** | Bottom-up final judgment; **Cannot** override Hard Veto |

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
                     (Execution — Phase 5 Mock / Alpaca Paper; orders off by default)
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

## Quick start

```bash
# 1. Python env
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 2. Config (add Alpaca paper keys for live paper trading)
cp .env.example .env

# 3. Tests
pytest tests/unit tests/integration -q
```

### A) Local without Docker (SQLite) — recommended if `docker` is missing

```bash
# Create local schema
python - <<'PY'
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from app.core.database import Base
import app.models  # noqa: F401

async def main() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///./investor_local.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()

asyncio.run(main())
PY

# Run API
DATABASE_URL="sqlite+aiosqlite:///./investor_local.db" \
  LOG_FORMAT=console \
  uvicorn app.main:app --reload --port 8000

# Or use the secure local start/stop helpers (loopback bind, local Postgres, pidfile, live-trading guard):
#   ./scripts/start.sh          # starts docker compose db (if needed) + API
#   ./scripts/stop.sh           # stops API + local Postgres
#   ./scripts/stop.sh --api-only
# Optional: INVESTOR_RELOAD=1 ./scripts/start.sh
#           INVESTOR_MANAGE_DB=0 ./scripts/start.sh   # API only

# Dashboard: http://127.0.0.1:8000/dashboard
```

Redis is **not required** for current phases (config only; unused at runtime).

### B) With Docker (PostgreSQL)

This project expects the Docker CLI. On macOS without Docker Desktop:

```bash
brew install colima docker docker-compose
colima start
mkdir -p ~/.docker/cli-plugins
ln -sfn "$(brew --prefix)/opt/docker-compose/bin/docker-compose" ~/.docker/cli-plugins/docker-compose
```

Then:

```bash
docker compose up -d db
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

Redis is optional and unused in current phases.

Default news/market providers are **stub** so analysis can run without news API keys.
Alpaca paper keys in `.env` are needed for real paper order/portfolio sync.

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

Allowlist / universe:

- `TRADE_ALLOWLIST` seeds the book and bounds unknown AI tickers.
- Default `UNIVERSE_MODE=dynamic`: AI **Universe Manager** keeps horizon groups
  (초단타/단타/단기/중기) and a small **focus set** for each session.
- See [docs/universe.md](docs/universe.md).

Default seed:
`SPY, QQQ, IWM, DIA, NVDA, MSFT, AMZN, GOOGL, META, AVGO, AMD, AAPL, TSLA, IONQ`

---

## Assumptions

1. **Agent firm, paper first:** Six agents run analysis → CIO decides → intents →
   paper broker when safety flags unlock. Live stays dual-gated off.
2. **Manual approval is optional:** `REQUIRE_MANUAL_ORDER_APPROVAL` is an ops brake,
   not the product identity. Ship defaults keep `ENABLE_BROKER_ORDERS=false`.
3. **LLM optional in unit tests:** Risk Engine and schema tests run without API keys.
4. **Stub/fixture providers** used until external data flags are enabled.
5. **Sector map** for concentration checks uses a static ETF/stock sector table
   (configurable later).
6. **ATR-based sizing** uses caller-supplied ATR/stop distance; collectors will
   populate these in Phase 2+.
7. **SQLite for unit tests** via `aiosqlite` when `DATABASE_URL` is unset in pytest.
8. **macOS + Linux** supported; Windows is best-effort only.

---

## Development phases

### Original delivery track (in-repo)

| Phase | Focus | Status |
|-------|--------|--------|
| 1 | Foundation, schemas, Risk Engine | Complete |
| 2–7 | Collection → agents → workflows → paper → dashboard | Implemented (MVP) |

### Roadmap track (current planning docs)

| Phase | Focus | Status |
|-------|--------|--------|
| 2 | Prompt system + agent framework hardening | Complete — `docs/phase2_report.md` |
| 3 | Calendar, DST, daily workflow SM, leases | Complete — `docs/phase3_report.md` |
| 4 | Data collection & normalization layer | Complete — `docs/phase4_report.md` |
| 5 | Broker & paper execution layer | Complete — `docs/phase5_report.md` |
| 6 | Intraday ops & position management | Complete — `docs/phase6_report.md` |
| 7 | Performance metrics, ops, dashboard, simulations | **This release** — `docs/phase7_report.md` |

Docs: `docs/performance_architecture.md`, `docs/operations_runbook.md`, `docs/security_audit_phase7.md`.

```bash
python -m app.cli performance portfolio
python -m app.cli operations metrics
python -m app.cli readiness evaluate
python -m app.cli simulation run --scenario bull-market --days 5
python -m app.cli backup create
curl -s localhost:8000/performance/risk | jq .
```

Phase 7 adds read-only performance/ops APIs and dashboard tabs. **Live trading remains NOT READY** — see `docs/live_trading_readiness_checklist.md`.

```bash
python -m app.cli intraday status
python -m app.cli positions monitor
python -m app.cli closing run
python -m app.cli postmarket settle
```

Defaults: `INTRADAY_OPERATION_MODE=OBSERVE_ONLY`, `ENABLE_BROKER_ORDERS=false`, `ENABLE_LIVE_TRADING=false`.
---

## License

Proprietary — internal use.
