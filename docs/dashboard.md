# Dashboard (Phase 7)

## Access

- `GET /dashboard` — static HTML (read-only)
- `GET /dashboard/summary` — overview JSON (existing)

## Tabs

| Tab | Data sources |
|-----|----------------|
| Overview | `/dashboard/summary` |
| Performance | `/performance/*` |
| Agents | `/performance/agents`, `/calibration`, `/decisions` |
| Operations | `/operations/metrics`, `/alerts`, `/readiness`, `/simulations` |
| Audit | `/status`, `/health`, `/decisions` |

## Safety

- **DEV ONLY** banner when `env` is development/test or live trading is blocked
- No credentials, full account IDs, or Live enable button
- `DASHBOARD_READ_ONLY=true` by default
- Workflow buttons (Premarket, Intraday, Emergency Stop) remain — they do not create arbitrary orders from the Performance tab

## Not supported

- Authentication / RBAC
- Write actions for alerts acknowledge from UI (use API/CLI)
- Embedded Prometheus graphs (use `/metrics` scrape target)
