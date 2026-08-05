# AI-managed universe (watchlist / focus)

## Goal

Stop treating `TRADE_ALLOWLIST` as the only tradable set. The firm maintains a **horizon-grouped watchlist** and a small **focus set** so agents do not review the entire market each session — while pursuing **max return / min loss** via style-appropriate selection.

## Horizons

| Code | Korean | Style |
|------|--------|--------|
| `scalp` | 초단타 | minutes–hours, ultra liquid |
| `day` | 단타 | same session |
| `short` | 단기 | multi-day swing |
| `medium` | 중기 | weeks–months |

Policies live in `app/universe/horizons.py` (capacity, re-eval cadence, liquidity bars, CIO `time_horizon` mapping).

## Modes

- `UNIVERSE_MODE=dynamic` (default): active watchlist gates **new entries**; collection uses focus ∪ holdings.
- `UNIVERSE_MODE=static`: legacy allowlist-only behavior.

`TRADE_ALLOWLIST` seeds the watchlist and acts as a soft boundary (unknown AI-invented tickers are rejected on add).

## APIs

- `GET /universe` — watchlist by horizon + latest focus
- `POST /universe/refresh` — run Universe Manager agent
- `GET /universe/horizons` — policy summaries

Dashboard Overview renders the same snapshot under **Universe** (mode, focus chips, active names by horizon).

## Scheduler

When `ENABLE_SCHEDULER=true` and dynamic mode is on, APScheduler also runs `universe_refresh` every `UNIVERSE_REFRESH_SECONDS` (default 900). Premarket workflow still refreshes once per session.

## Persistence

- `watchlist_symbols`
- `focus_set_snapshots`

Migration: `0007_universe_watchlist`.
