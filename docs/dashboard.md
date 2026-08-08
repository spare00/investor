# Dashboard (Phase 7)

## Access

- `GET /dashboard` — static HTML (read-only)
- `GET /dashboard/summary` — overview JSON (existing)

## Tabs

| Tab | Data sources | UI |
|-----|----------------|-----|
| Overview | `/dashboard/summary` | Session, portfolio, risk, CIO, positions/orders, jobs, compact universe + ops strip (hard/force/alerts) |
| Performance | `/performance/*` | Portfolio / returns / risk / drawdown / trades |
| Agents | `/performance/agents`, `/calibration`, `/decisions` | Attribution KPIs, horizon/agent slices, decision evals with `summary.by_horizon` |
| Operations | `/dashboard/summary` + `/operations/*` | Monitor, force-close, settlement/recon/overnight, recovery, alerts, LLM budget, universe horizons, ops KPIs |
| Audit | `/status`, `/health`, `/decisions` | Status pills, risk caps, decision audit table |

Overview and the top strip show **US equity session phase** (`REGULAR`, `PREMARKET`, `AFTER_HOURS`, …) from `MarketCalendarService` via `market_status.us_session`, plus operator trading controls and daily workflow state when present. `session_jobs` lists DB-planned daily jobs (not only APScheduler pollers in `next_jobs`).

Each non-Overview panel keeps a collapsed **Raw JSON** details block for debugging.

## Timestamps (GUI)

Storage stays UTC. The dashboard **never shows UTC**.

| Clock | Zone | Used for |
|-------|------|----------|
| **ET** | `America/New_York` | Market session, jobs, settlement/recon/closing, news, performance as-of, decision evals |
| **BNE** | `Australia/Brisbane` | Ops logs, alerts, errors, recovery, LLM budget day, agent-run stamps |

## Safety

- **DEV ONLY** banner when `env` is development/test or live trading is blocked
- No credentials, full account IDs, or Live enable button
- `DASHBOARD_READ_ONLY=true` by default
- Workflow buttons (Premarket, Intraday, Emergency Stop) remain — they do not create arbitrary orders from the Performance tab

## Not supported

- Authentication / RBAC
- Embedded Prometheus graphs (use `/metrics` scrape target)
