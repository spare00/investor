# Context Builders

`MarketIntelligenceContextBuilder`, `MacroContextBuilder`, `QuantContextBuilder`,
`RevalidationContextBuilder`, `IntradayContextBuilder`.

- Cutoff filtering
- Size limits (`MAX_NEWS_CONTEXT_ITEMS`, etc.)
- Source IDs + quality + conflicts
- External text wrapped via `wrap_untrusted`
- Raw provider JSON is not exposed to agents (`provider_formats_exposed: false`)
