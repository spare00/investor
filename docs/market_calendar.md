# Market Calendar

## Library choice

**`exchange_calendars`** (calendar code `XNYS` for NYSE, `XNAS` for Nasdaq).

### Why

- Holidays and early closes are data-driven (not hand-maintained month rules).
- Session open/close are timezone-aware and reflect US DST automatically via the calendar’s UTC timestamps converted with `zoneinfo.ZoneInfo("America/New_York")`.
- Already a project dependency; avoids reinventing holiday tables.

### Limits

- Equity regular session oriented; extended hours (4:00–20:00 ET) are **assumed** locally for premarket/postmarket windows, not exchange-authoritative for every venue.
- Futures / options calendars are out of scope.
- Calendar name mapping: `NYSE`/`NASDAQ` → `XNYS`/`XNAS`; other codes pass through.

## Service API

`app.market.calendar.MarketCalendarService`

| Method | Purpose |
|--------|---------|
| `is_trading_day(date)` | Session membership |
| `get_session(date)` | Full session info |
| `get_next_trading_day` / `get_previous_trading_day` | Navigation |
| `get_next_market_open` / `get_next_market_close` | Forward lookup |
| `get_market_status(now)` | Phase + ET/BNE labels |
| `get_schedule(start, end)` | Inclusive day range |

Naive datetimes are treated as UTC at the boundary (`_ensure_aware`).

## Timezones

| Role | Zone |
|------|------|
| Storage | UTC |
| Market session math | `America/New_York` (DST via IANA) |
| Operator display | `Australia/Brisbane` (no DST) |

Do **not** compute market hours with fixed `UTC-4` / `UTC-5` / `UTC+10` offsets alone.
