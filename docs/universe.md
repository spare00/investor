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

`TRADE_ALLOWLIST` seeds the watchlist. A curated **candidate pool** (or `UNIVERSE_CANDIDATE_POOL`) expands what the AI may add; unknown invented tickers are still rejected.

## Closing / overnight

- Watchlist horizons `scalp` / `day` are treated as intraday-only at the closing window (force flatten), even if `overnight_allowed` was mis-set.
- New entries are skipped in the closing / force-close window when `ALLOW_NEW_POSITIONS_IN_CLOSING_WINDOW=false` (exits still validate).
- Intraday eval inside the force-close window also runs `ClosingService` to create exit intents (and optional paper submits when `AUTO_EXECUTE_FORCE_CLOSE=true` plus paper automation flags).
- Candidate pool can be theme-ranked (`tech`, `ai`, `risk_on`, …) when refresh receives themes / market_regime.

## APIs

- `GET /universe` — watchlist by horizon + latest focus
- `POST /universe/refresh` — run Universe Manager agent
- `GET /universe/horizons` — policy summaries

CLI: `investor universe show|horizons|refresh`

Dashboard Overview renders the same snapshot under **Universe** (mode, focus chips, active names by horizon) with a Refresh Universe control.

## Scheduler

When `ENABLE_SCHEDULER=true` and dynamic mode is on, APScheduler also runs `universe_refresh` every `UNIVERSE_REFRESH_SECONDS` (default 900). Premarket workflow still refreshes once per session, using the prior CIO/MI **regime/themes** when available, then applies post-analysis priority boosts (no second LLM).

## Persistence

- `watchlist_symbols`
- `focus_set_snapshots`

Migration: `0007_universe_watchlist`.
