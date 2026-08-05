# Event Bus

`IntradayEventBus` persists `intraday_events` with deduplication keys, revision on duplicates, priority map, and reanalysis rate limits (`MIN_GLOBAL_REANALYSIS_GAP_MINUTES`, `MAX_INTRADAY_REANALYSES`, per-symbol caps).

Risk/stop/emergency events set `bypass_cooldown=True`.

Dedup bumps `revision` and records `dedupe_hits` but **keeps** `NEW`/`QUEUED` status so pending drains (monitor / news bridge) are not starved by repeated publishes.

`ingest_high_importance_news` (`app/intraday/news_bridge.py`) scans recent `news_items` (lookback `INTRADAY_NEWS_LOOKBACK_MINUTES`, default 90) and publishes `HIGH_IMPORTANCE_NEWS` / earnings-style events with `requires_analysis=true`. Unattended `evaluate_intraday` runs this every tick and escalates to `news_high_importance` (or `risk_change` when monitor/stop events are also pending).
