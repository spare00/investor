# Decision Evaluation

## Goal

Score CIO/intraday decisions **after the fact** using only prices available at the evaluation horizon (no look-ahead).

## Module

`app/performance/decision_eval.py` — evaluates BUY/SELL/HOLD/NO_TRADE against horizon price and optional benchmark return.

`app/performance/price_lookup.py` — resolves `decision_price` / `horizon_price` from `market_snapshots` using book hold windows.

## Evaluation horizons (by universe book)

| Book | Window | Label |
|------|--------|-------|
| scalp | 4h | `4h` |
| day | ~1 session (390m) | `1session` |
| short | 10d | `10d` |
| medium | 60d | `60d` |
| unknown | 1d | `1d` |

If `now < decision_ts + window`, horizon price stays **pending** (not scored early).

## Price resolution order

**Decision price:** payload → market snapshot at/before decision (within skew) → entry_zone mid  
**Horizon price:** payload → latest snapshot in `[decision_ts, horizon_end]`  
**Benchmark return:** same window on `primary_benchmark` (default SPY)

Audit persist stamps `decision_price` / `universe_horizon` onto symbol plans when snapshots exist.

## API

- `GET /performance/decisions` — evaluations + `summary.by_horizon` + `price_resolution` counts
- `POST /performance/evaluate-decisions` — batch evaluate + optional persist (`price_at_horizon`, `evaluation_horizon`, status PENDING/AVAILABLE/UNAVAILABLE)

## Horizon attribution

- Portfolio-level rows get a dominant `universe_horizon` from symbol plans
- Symbol-level rows expand from `symbol_actions`
- `summary.by_horizon` reports directional hit-rate and avg quality per book

## Limitations

- Sparse historical `market_snapshots` still leave long horizons UNAVAILABLE until enough session ticks accumulate
- Live broker historical bars are not used for backfill (intraday/premarket collect now persists market prints)
- Postmarket re-eval uses `decision_eval_lookback_days` (default 90) on a self-rescheduling `postmarket_eval` job per venue so leftover slices continue instead of being dropped on timeout
- Does not auto-close positions or change future agent prompts
