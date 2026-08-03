# News Deduplication

Deterministic pipeline in `app/data_quality/news_dedup.py`:

1. provider article ID
2. canonical URL
3. normalized headline hash
4. similar headline + overlapping symbols within time window
5. event fingerprint clusters (`CanonicalNewsEventCluster`)

No LLM required for dedup. Corrections update cluster membership; members are not silently deleted.
