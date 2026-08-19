# Operations Runbook (Phase 7)

Commands match `python -m app.cli` and HTTP APIs.

## Paper firm arming (unattended)

Fail-closed defaults keep orders off. To run the 6-agent paper loop:

1. `.env`: `TRADING_MODE=paper`, `BROKER_ENVIRONMENT=paper`, `INTRADAY_OPERATION_MODE=PAPER_AUTOMATED`
2. Unlock paper submits: `ENABLE_BROKER_ORDERS=true`, `ENABLE_AUTOMATED_EXECUTION=true`, `REQUIRE_MANUAL_ORDER_APPROVAL=false`
3. Scheduler: `ENABLE_SCHEDULER=true` (plans `intraday_eval_*`, `closing_window`, optional `universe_refresh`)
4. Monitoring: `ENABLE_INTRADAY_MONITORING=true` (stop/TP ticks escalate to CIO on each eval)
5. Hard stops and closing-window flatten: `INTRADAY_OPERATION_MODE=PAPER_AUTOMATED` implies both. `AUTO_EXECUTE_HARD_STOPS` / `AUTO_EXECUTE_FORCE_CLOSE` only arm those exits in other modes (e.g. MANUAL_APPROVAL).
6. Universe: `UNIVERSE_MODE=dynamic`, `UNIVERSE_MANAGER_ENABLED=true`

### Embedded local LLM (no OpenAI spend cap)

To drop the monthly AUD / daily token cap, run inference on this Mac:

```bash
./scripts/ensure_local_llm.sh          # brew install ollama, serve, pull qwen2.5:14b
                                       # derived qwen2.5:14b-ctx; request window is 8k
```

`.env`:

```
LLM_RUNTIME=local
LLM_LOCAL_MODEL=qwen2.5:14b-ctx
LLM_LOCAL_NUM_CTX=8192
LLM_LOCAL_MAX_TOKENS=800
```

Python owns indicators and Hard Vetoes. Local LLM is reserved for MI (themes), Macro (regime), Devil (yes/no), and CIO (actions). Quant and Risk skip chat and use engines. Each agent has its own `num_ctx` / `max_tokens` / model slot (`GET /health` → `agent_roles`). Ollama still benefits from a derived `*-ctx` model so the *maximum* window is large enough; each request sends a smaller `num_ctx` (4k–8k) so 14B can finish inside the 8-minute job cap. Scheduler job timeout stays 8 minutes for local and cloud. `GET /health` should show `llm_is_local: true`. Cloud `gpt-*` model names are rewritten to the local model. Intraday cadence then follows horizon policy (scalp ~2m) instead of the 12-call spend floor.

### Switch back to cloud GPT (keep the same pipeline)

Do **not** delete local knobs. Flip the runtime:

```
LLM_RUNTIME=cloud
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=...
LLM_MODEL=gpt-4o-mini
```

Then restart the API. Quant and Risk resume chat; Macro∥Quant run in parallel; the AUD/token budget applies again. `GET /health` should show `llm_is_local: false`.

### Committee wall-time watch

As managed symbols grow, local 14B evals can creep toward the 8-minute `job_action_timeout` and start failing as `job_action_timeout:480s`. The dashboard and `/metrics` exist so that shows up before it is frequent.

| Level | Meaning |
|-------|---------|
| `ok` | Last finished `intraday_eval` used under 70% of the cap, no timeouts this session |
| `warn` | Last eval used ≥ 70% of the cap (headroom thinning) |
| `bad` | A timeout this session, or last eval used ≥ 90% of the cap |

Where to look:

- Overview cadence pills (`LLM local/cloud`, `eval Ns / 480s`, headroom %, watch/focus counts, timeout count)
- Operations → LLM Budget → recent eval wall times (`Took`)
- Session jobs table **Took** column
- `GET /dashboard/summary` → `committee_watch`

```bash
curl -s localhost:8000/dashboard/summary | jq '{
  llm_is_local: .committee_watch.llm_is_local,
  last_s: .committee_watch.last_eval_seconds,
  cap: .committee_watch.timeout_cap_seconds,
  headroom_pct: .committee_watch.headroom_pct,
  level: .committee_watch.level,
  timeouts: .committee_watch.timeouts_session,
  watch: .committee_watch.watchlist_symbols,
  focus: .committee_watch.focus_symbols
}'
curl -s localhost:8000/metrics | rg 'investor_(last_committee_seconds|committee_headroom_ratio|scheduler_job_timeouts_total|watchlist_symbols|focus_symbols)'
```

If timeouts become frequent: shrink focus/watch (Universe Manager / allowlist), keep Quant/Risk on Python locally, do not raise the 8-minute cap as the first move, or switch `LLM_RUNTIME=cloud` if GPU wall time is the bottleneck.

Verify:

```bash
python -m app.cli daily-workflow prepare
python -m app.cli scheduler list
python -m app.cli universe show
python -m app.cli closing run   # or wait for closing_window job
curl -s localhost:8000/dashboard/summary | jq '{workflow:.market_status.workflow,force_close,hard_stop,settlement:.latest_settlement,recon:.latest_reconciliation,recovery:.latest_recovery,alerts:(.active_alerts|length),session_jobs:(.session_jobs|length),committee_watch}'
```

Overview shows reeval budget (`reeval used/max`), planned interval, session job plan, paused hygiene names, force-close arming, latest recovery, and active operational alerts.

## Daily observe (paper)

```bash
python -m app.cli market-status
python -m app.cli daily-workflow status
python -m app.cli intraday status
curl -s localhost:8000/dashboard/summary | jq .
```

## Performance review

```bash
python -m app.cli performance portfolio
python -m app.cli performance risk
python -m app.cli performance drawdowns
python -m app.cli performance trades
python -m app.cli performance agents
python -m app.cli performance recalculate
curl -s -X POST localhost:8000/performance/recalculate | jq .
```

## Providers & ops

```bash
python -m app.cli providers health
python -m app.cli providers reliability
python -m app.cli operations metrics
python -m app.cli alerts list
python -m app.cli alerts acknowledge --id <uuid>
```

## Readiness (LIVE blocked)

```bash
python -m app.cli readiness evaluate
curl -s localhost:8000/operations/readiness | jq .
curl -s -X POST localhost:8000/readiness/evaluate | jq .
```

## Simulation

```bash
python -m app.cli simulation run --scenario bull-market --days 5
python -m app.cli simulation report --id <uuid>
```

## Backup

```bash
python -m app.cli backup create
python -m app.cli backup verify --path backups/<id>.zip
```

## Security

```bash
python -m app.cli security
```

## Emergency

```bash
curl -X POST localhost:8000/trading/emergency-stop
python -m app.cli recovery run
```

Live trading enablement is **not** in this runbook — see `live_trading_readiness_checklist.md` (NOT READY).
