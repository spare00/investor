# AI-managed universe (watchlist / focus)

## Goal

Stop treating `TRADE_ALLOWLIST` as the only tradable set. The firm maintains a **horizon-grouped watchlist** and a small **focus set** so agents do not review the entire market each session — while pursuing **max return / min loss** via style-appropriate selection.

## Horizons

| Code | Korean | Style | Re-eval |
|------|--------|--------|---------|
| `scalp` | 초단타 | minutes–hours, ultra liquid | ~2m |
| `day` | 단타 | same session | ~5m |
| `short` | 단기 | multi-day swing | ~15m |
| `medium` | 중기 | weeks–months | ~60m |

Policies live in `app/universe/horizons.py` (capacity, re-eval cadence, liquidity bars, stop ATR/pct widths, overnight defaults, CIO `time_horizon` mapping). Agents (MI / Quant / Risk / Devil / CIO) receive enriched watchlist rows with those fields. Missing CIO stops are filled with book-aware ATR/pct widths (scalp tight → medium wide), not a flat 1–2%. Intraday cooldowns use the **tightest** open book’s `reeval_seconds` (see `app/universe/reeval.py`). Session job plans (`intraday_eval_*`) use the tightest **active watchlist** horizon, floored by LLM budget (`≈ 1.5 × MAX_INTRADAY_REANALYSES` ticks per session); `INTRADAY_REEVALUATION_INTERVAL_MINUTES` is the fallback when no horizons are known. Universe refresh (scheduler, `POST /universe/refresh`, premarket) replans pending ticks. News collection lookback uses the longest book among symbols under review.

## Modes

- `UNIVERSE_MODE=dynamic` (default): active watchlist gates **new entries**; collection uses focus ∪ holdings.
- `UNIVERSE_MODE=static`: legacy allowlist-only behavior.

`TRADE_ALLOWLIST` seeds the US watchlist. `TRADE_ALLOWLIST_AU` is the ASX entry allowlist (separate book; empty disables AU new entries). Venue routing uses `PRIMARY_VENUE` / `ENABLED_VENUES` — see `docs/market_calendar.md`.

## Closing / overnight

- Watchlist horizons `scalp` / `day` are treated as intraday-only at the closing window (force flatten), even if `overnight_allowed` was mis-set.
- `short` may hold overnight in a quiet tape, but earnings / macro events / holidays prefer flatten (`overnight_event_strict`); elevated gap risk prefers flatten on short vs reduce-on-medium.
- `medium` overnight is the default; event/gap risk → manual review or size reduction, not automatic flatten.
- New order intents stamp `exit_policy.overnight_allowed` / `closing_policy` from the symbol’s watchlist horizon (not hardcoded false).
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

When `ENABLE_SCHEDULER=true` and dynamic mode is on, APScheduler also runs `universe_refresh` every `UNIVERSE_REFRESH_SECONDS` (default 7d backup). **Universe Manager LLM** is capped by `UNIVERSE_REFRESH_MIN_INTERVAL_DAYS` (default **7**): between LLM runs, premarket/scheduler only rebuild focus + hygiene without the model so daily trading budget is not burned on watchlist churn. With `UNIVERSE_REFRESH_SESSION_ONLY=true` (default), scheduler ticks skip overnight `BEFORE_PREMARKET`. Premarket still applies post-analysis priority boosts (no second LLM). Manual `POST /universe/refresh` with `{"force": true}` bypasses the weekly gate.

## Persistence

- `watchlist_symbols` (optional `payload.last_outcome_stats` from closed-trade feedback)
- `focus_set_snapshots`

Migration: `0007_universe_watchlist`.

## Outcome feedback (observational)

Universe refresh passes `recent_outcomes` (90d closed lifecycles by symbol / horizon / seed source) into the Universe Manager. Stats are stamped onto watchlist payload for ops visibility (`GET /universe` → `recent_outcomes`). Priority/pause decisions remain LLM/human — no automatic strategy mutation.
