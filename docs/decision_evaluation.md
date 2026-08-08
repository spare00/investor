# Decision Evaluation

## Goal

Score CIO/intraday decisions **after the fact** using only prices available at the evaluation horizon (no look-ahead).

## Module

`app/performance/decision_eval.py` — evaluates BUY/SELL/HOLD/NO_TRADE against horizon price and optional benchmark return.

## API

- `GET /performance/decisions` — evaluations for period plus `summary.by_horizon`
- `POST /performance/evaluate-decisions` — batch evaluate + optional persist to `decision_evaluations`

## Horizon attribution

- Portfolio-level rows get a dominant `universe_horizon` from symbol plans
- Symbol-level rows expand from `symbol_actions` with horizon from plan stamp / watchlist / CIO `time_horizon` map
- `summary.by_horizon` reports directional hit-rate and avg quality per book

## Limitations

- Most historical decisions lack `horizon_price` in payload → status `UNAVAILABLE`
- Intraday decisions not fully wired to price-at-horizon fetch
- Does not auto-close positions or change future agent prompts
