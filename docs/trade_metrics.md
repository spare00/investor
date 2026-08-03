# Trade Metrics

## Scope

Closed-trade analytics: win rate, profit factor, average holding time, risk-adjusted PnL per trade.

## Data sources

1. `PositionLifecycle` rows with `status=CLOSED` in the requested period
2. Fallback: `TradePnL` table when no closed lifecycles

## API

- `GET /performance/trades`
- CLI: `python -m app.cli performance trades`

## Not supported

- Round-trip matching against broker fills at execution granularity (uses lifecycle realized_pl)
- MAE/MFE stored per trade in DB (computed in module, not always persisted)
- Options or multi-leg trade decomposition
