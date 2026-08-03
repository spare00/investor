# Portfolio Metrics

## Canonical valuation

`PortfolioValuationRecord` (`portfolio_valuations`) is the target store for mark-to-market snapshots.

Fields include cash, long/short market value, gross/net exposure, equity, buying power, daily PnL components, benchmark_values JSON, and source_snapshot_ids for audit.

Unique key: `(portfolio_id, as_of, valuation_kind)`.

## Current behavior

- `GET /performance/portfolio` builds valuation from latest `PortfolioSnapshot` when available
- Falls back to `DailyPerformance` equity curve when snapshots are sparse
- `build_portfolio_valuation()` is pure/deterministic

## Limitations

- Dual legacy stores (`DailyPerformance`, `PortfolioSnapshot`) still exist; Phase 7 valuation records are populated on explicit recalculate paths only
- Buying power and net liquidation value default from snapshot payload when broker fields absent
- No intraday sub-minute valuation series
