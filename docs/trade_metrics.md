# Trade Metrics

## Scope

Closed-trade analytics: win rate, profit factor, average holding time, risk-adjusted PnL per trade.

## Data sources

1. `PositionLifecycle` rows with `status=CLOSED` in the requested period
2. Fallback: `TradePnL` table when no closed lifecycles

## API

- `GET /performance/trades` — firm metrics plus `by_horizon` slices (`scalp` / `day` / `short` / `medium` / `unknown`)
- CLI: `python -m app.cli performance trades`

## Horizon attribution

- Closed `PositionLifecycle` rows carry `exit_policy.horizon` when stamped from the watchlist book (with watchlist fallback at read time).
- Each book gets the same metric set as the firm aggregate (win rate, expectancy, profit factor, …).
- Empty books return `INSUFFICIENT_DATA` metrics with `trade_count=0`.

## Not supported

- Round-trip matching against broker fills at execution granularity (uses lifecycle realized_pl)
- MAE/MFE stored per trade in DB (computed in module, not always persisted)
- Options or multi-leg trade decomposition
- Auto-tuning strategy or risk from trade metrics (observational / ops only)
