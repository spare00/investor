# Performance Architecture (Phase 7)

## Purpose

Deterministic portfolio analytics, agent attribution, and operational KPIs — **no LLM calls**, **no strategy mutation**.

```
Portfolio snapshots / lifecycles / trade PnL
        ↓
PerformanceService (orchestrator)
        ↓
Submodules: valuation, returns, risk, drawdown, trades, execution_quality,
            decision_eval, agent_eval, calibration, providers, operational
        ↓
Optional persistence → Phase 7 ORM tables (portfolio_valuations, performance_metrics, …)
        ↓
GET /performance/* APIs + dashboard Performance tab
```

## What is supported

- On-demand recalculation over a date range (`POST /performance/recalculate`)
- Read APIs for portfolio, returns, risk, drawdowns, trades, execution, decisions, agents, calibration, providers
- Idempotent-ish recalculate: same period returns cached run id in-process
- Benchmark alignment via fixture/synthetic series (`load_and_align`)

## What is NOT supported

- Real-time streaming performance updates
- Automatic nightly metric jobs (manual API/CLI only)
- Full persistence of every metric row on every GET (most endpoints compute on read)
- Live benchmark ingestion from paid data vendors
- Strategy parameter auto-tuning from performance results

Live trading remains **hard-blocked** in Phase 7.
