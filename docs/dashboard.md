# Dashboard (Phase 7)

## Access

- `GET /dashboard` — static HTML (read-only)
- `GET /dashboard/summary` — overview JSON (existing)

## Tabs

| Tab | Data sources | UI |
|-----|----------------|-----|
| Overview | `/dashboard/summary` | US session, LLM budget panel, universe, force-close, settlement/recon/overnight, startup recovery, active alerts (ack/resolve), monitor, reeval cadence, session jobs, agent lamps |
| Performance | `/performance/*` | Metric KPIs, holdings / drawdown tables, Raw JSON toggle |
| Agents | `/performance/agents`, `/calibration`, `/decisions` | Accuracy KPIs, calibration bars, eval table |
| Operations | `/operations/metrics`, `/alerts`, `/readiness`, `/simulations` | Ops KPIs, readiness checklist, sim table |
| Audit | `/status`, `/health`, `/decisions` | Status pills, risk caps, decision audit table |

Overview and the top strip show **US equity session phase** (`REGULAR`, `PREMARKET`, `AFTER_HOURS`, …) from `MarketCalendarService` via `market_status.us_session`, plus operator trading controls and daily workflow state when present. `session_jobs` lists DB-planned daily jobs (not only APScheduler pollers in `next_jobs`).

Each non-Overview panel keeps a collapsed **Raw JSON** details block for debugging.

## Safety

- **DEV ONLY** banner when `env` is development/test or live trading is blocked
- No credentials, full account IDs, or Live enable button
- `DASHBOARD_READ_ONLY=true` by default
- Workflow buttons (Premarket, Intraday, Emergency Stop) remain — they do not create arbitrary orders from the Performance tab

## Not supported

- Authentication / RBAC
- Embedded Prometheus graphs (use `/metrics` scrape target)
