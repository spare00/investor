# Market Calendar

## Library choice

**`exchange_calendars`** — `XNYS` (NYSE / US) and `XASX` (ASX / AU).

### Why

- Holidays and early closes are data-driven (not hand-maintained month rules).
- Session open/close are timezone-aware and reflect DST automatically via the calendar’s UTC timestamps converted with `zoneinfo`.
- Already a project dependency; avoids reinventing holiday tables.

### Limits

- Equity regular session oriented; extended hours are **assumed** locally (US 4:00–20:00 ET; ASX pre-open from 07:00 Sydney) and are not exchange-authoritative for every product.
- Futures / options calendars are out of scope.
- Calendar name mapping: `NYSE`/`NASDAQ` → `XNYS`/`XNAS`; `ASX` → `XASX`; other codes pass through.

## Venues

`PRIMARY_VENUE=US|AU` selects the default book. Explicit `venue=` on the calendar API / service overrides it.

| Venue | MIC / calendar | Market TZ | Currency | IBKR exchange hint |
|-------|----------------|-----------|----------|--------------------|
| `US` | XNYS | America/New_York | USD | SMART |
| `AU` | XASX | Australia/Sydney | AUD | SMART (primary ASX); direct `ASX` is a fallback |

ASX paper: prefer SMART/AUD qualification. Direct ASX routing can hit Gateway precautionary **error 10311** unless allowed under API Precautionary Settings.

Sessions do not overlap in BNE wall-clock time (ASX daytime / US overnight), which is the ops rationale for dual-book scheduling later. Capital, FX, and universe are still separate concerns.

### Ops: turn on dual book

```bash
ENABLED_VENUES=US,AU
PRIMARY_VENUE=US          # default order routing / briefing calendar
TRADE_ALLOWLIST_AU=BHP,CBA,VAS,IOZ,NDQ,JPEQ
```

Watchlist seeding adds missing AU allowlist symbols with `payload.venue=AU`. Daily analysis for an AU run uses `entry_universe(venue=AU)`. IBKR closes stamp venue from position exchange/currency.

Registry: `app.market.venues` (`venue_for_symbol`, `enabled_venues`). Factory: `MarketCalendarService(..., venue=)` / `get_market_calendar(venue=)`.

## Service API

`app.market.calendar.MarketCalendarService`

| Method | Purpose |
|--------|---------|
| `is_trading_day(date)` | Session membership |
| `get_session(date)` | Full session info (includes `venue`) |
| `get_next_trading_day` / `get_previous_trading_day` | Navigation |
| `get_next_market_open` / `get_next_market_close` | Forward lookup |
| `get_market_status(now)` | Phase + ET/BNE + market-local labels |
| `get_schedule(start, end)` | Inclusive day range |

Naive datetimes are treated as UTC at the boundary (`_ensure_aware`).

HTTP:

- `GET /market/status?venue=US|AU`
- `GET /market/calendar?venue=AU&day=2026-08-10`
- `GET /market/venues`

## Timezones

| Role | Zone |
|------|------|
| Storage | UTC |
| Market session math | Venue timezone (`America/New_York` or `Australia/Sydney`) |
| Operator display | `Australia/Brisbane` (no DST) |
| Dashboard ET label | `America/New_York` (always present on status snapshots) |

Do **not** compute market hours with fixed `UTC-4` / `UTC-5` / `UTC+10` offsets alone.
