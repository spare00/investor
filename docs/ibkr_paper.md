# IBKR Paper Trading (TWS API via IB Gateway)

> Cutover from Alpaca. Tag `alpaca-paper-final` marks the last Alpaca-centric tree.

## Prerequisites

1. IB Gateway (or TWS) installed and logged in as **Paper Trading**
2. API Type: **IB API** (not FIX CTCI)
3. Configure → API → Settings:
   - Socket port (Gateway paper default **4002**)
   - Read-Only API **off** for order tests
   - Trusted IPs include `127.0.0.1`
4. Leave Gateway running while the app is connected

Passwords stay in Gateway. The app only needs host/port/clientId/(optional) account id.

## Settings

```bash
BROKER_PROVIDER=ibkr
BROKER_ENVIRONMENT=paper
ENABLE_BROKER_CONNECTION=true
# Orders still fail-closed until you arm:
# ENABLE_BROKER_ORDERS=true
# ENABLE_AUTOMATED_EXECUTION=true

IBKR_HOST=127.0.0.1
IBKR_PORT=4002
IBKR_CLIENT_ID=1
IBKR_ACCOUNT=DUR804020   # optional if only one managed account
IBKR_DEFAULT_EXCHANGE=SMART
IBKR_DEFAULT_CURRENCY=USD
IBKR_ALLOW_LIVE_PORTS=false
```

Live-looking ports `4001` / `7496` are refused unless `IBKR_ALLOW_LIVE_PORTS=true`.

## Market data

Set `MARKET_DATA_PROVIDER=ibkr` (and keep `ENABLE_EXTERNAL_DATA` /
`ENABLE_MARKET_DATA_COLLECTION` on) so collection and execution sizing use Gateway
quotes. Market-data polls use `IBKR_MD_CLIENT_ID` (default 11) so they do not
collide with the broker `IBKR_CLIENT_ID`.

```bash
# Gateway paper must be up on IBKR_PORT
python -m app.cli broker ping
python -m app.cli broker account
python -m app.cli broker positions
```

## Dependency

```bash
pip install -e ".[ibkr]"
# or base install — ib_async is in main dependencies
```

## Notes

- First cutover is **US equities** via `SMART`/`USD`. Pass `OrderRequest.venue="AU"` (or set `PRIMARY_VENUE=AU`) so qualification prefers `SMART`/`AUD` (ASX primary). Direct `ASX` routing may require Gateway precautionary allow (error 10311). Dual-book scheduler: set `ENABLED_VENUES=US,AU` — see `docs/scheduler_and_leases.md`.
- Existing Alpaca paper history does not transfer — treat IBKR paper as day 0.
- `clientId` must be unique per Gateway connection (avoid colliding with TWS UI tools).
- Alpaca adapter remains in-tree for rollback (`BROKER_PROVIDER=alpaca`) but is no longer the cutover path.
- Outside RTH, IB may emit **Warning 399** (`ValidationError` → held until open). We map that to `accepted` / working, not rejected.
- Paper order smoke (far limit buy → open → cancel) was verified via `IbkrBroker` and `OrderManager`. Cancel resting test orders; do not leave far limits overnight.
