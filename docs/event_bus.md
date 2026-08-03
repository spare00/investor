# Event Bus

`IntradayEventBus` persists `intraday_events` with deduplication keys, revision on duplicates, priority map, and reanalysis rate limits (`MIN_GLOBAL_REANALYSIS_GAP_MINUTES`, `MAX_INTRADAY_REANALYSES`, per-symbol caps).

Risk/stop/emergency events set `bypass_cooldown=True`.
