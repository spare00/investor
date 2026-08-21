# AI-managed universe (watchlist / focus)

## Goal

Stop treating `TRADE_ALLOWLIST` as the only tradable set. Weekend Universe Manager maintains an **index-like membership** (seed ∪ liquid candidates, grouped by industry — Nasdaq-100 / S&P-500 style book-keeping, not a full-market scan) and picks a **working set** of ~10 names. Weekday CIO uses that working set with tape, news, and horizon playbooks (scalp / day / short; medium is hold-only).

## Horizons

| Code | Korean | Style | Re-eval |
|------|--------|--------|---------|
| `scalp` | 초단타 | minutes–hours, ultra liquid | ~2m |
| `day` | 단타 | same session | ~5m |
| `short` | 단기 | multi-day swing | ~15m |
| `medium` | 중기 | weeks–months | ~60m |

Policies live in `app/universe/horizons.py`. **Entry/exit rules** live in `app/universe/book_strategy.py` so 초단타 / 단타 / 단기 are not one 2% continuation model. Scalp is tape (price + volume acceleration, last above sma20); day is session structure (last vs typical/open); short is SMA swing. RSI is a haircut, not a hard entry gate. New size is **risk-budget first** (`risk_budget_pct` / ATR stop), with `target_size_pct` only a notional cap. 중기 is ignored for new entries and research focus (existing medium holdings are still held). Quant Python and CIO fallback (and LLM briefs) apply the matching playbook per symbol. Intraday job cadence uses the tightest **active strategy** book in open names or focus (scalp ~2m, day ~5m, short ~15m); medium is not used to slow or densify the plan. Cloud still floors spacing by the token budget. News lookback uses the longest book among symbols under review.

## Modes

- `UNIVERSE_MODE=dynamic` (default): **new entries** = active watchlist ∩ membership (seed ∪ curated candidates). Collection = venue-scoped focus ∪ holdings.
- `UNIVERSE_MODE=static`: legacy allowlist-only behavior.

`TRADE_ALLOWLIST` / `TRADE_ALLOWLIST_AU` **seed** membership. Weekend promotion from the curated candidate pool is buyable next week — the frozen `.env` list is no longer an entry ceiling in dynamic mode.

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

When `ENABLE_SCHEDULER=true` and dynamic mode is on, APScheduler polls `universe_refresh` every `UNIVERSE_REFRESH_SECONDS` (default **6h**). **Universe Manager LLM** (membership + working-set pick) runs only when:

1. `UNIVERSE_REFRESH_WEEKEND_ONLY=true` (default) — operator TZ weekend (Sat/Sun, default `Australia/Brisbane`), and
2. at least `UNIVERSE_REFRESH_MIN_INTERVAL_DAYS` (default **7**) since the last LLM focus snapshot.

The weekend tick passes last CIO regime + Market Intelligence themes, the sector-grouped membership, and 90d outcomes. It does **not** scan the whole market. Between LLM runs (weekdays), premarket/scheduler only rebuild venue-scoped focus + hygiene. Manual `POST /universe/refresh` with `{"force": true}` bypasses weekend + weekly gates.

Dual-book: seed = `TRADE_ALLOWLIST` ∪ `TRADE_ALLOWLIST_AU`; curated candidates include liquid US + ASX names when `ENABLED_VENUES` includes AU. Entry/collection remain venue-scoped.


## Persistence

- `watchlist_symbols` (optional `payload.last_outcome_stats` from closed-trade feedback)
- `focus_set_snapshots`

Migration: `0007_universe_watchlist`.

## Outcome feedback (observational)

Universe refresh passes `recent_outcomes` (90d closed lifecycles by symbol / horizon / seed source) into the Universe Manager. Stats are stamped onto watchlist payload for ops visibility (`GET /universe` → `recent_outcomes`). Priority/pause decisions remain LLM/human — no automatic strategy mutation.
