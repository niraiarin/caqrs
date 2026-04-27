# Data source license & ToS audit

This document tracks the legal status of every data source CAQRS ingests.
**It must be kept up to date** — a future CI lint will fail builds when a
new data source appears in `src/` without a corresponding entry here.

| Source       | Type | License / ToS | Allowed use | Rate limits | Last reviewed |
| ------------ | ---- | ------------- | ----------- | ----------- | ------------- |
| _(none yet — P0 has no data ingestion; first entries land in P1)_                                  |

## Review checklist

When adding a new data source, fill in:

- [ ] Source name and provider URL.
- [ ] License or ToS URL (link to the canonical document, with retrieval date).
- [ ] Allowed use (research / commercial / redistribution / derivative works).
- [ ] Rate limits and cost (free tier / paid tier).
- [ ] Required attribution and the exact attribution string.
- [ ] Personal data / GDPR considerations (PRAW exposes pseudonymous user data).
- [ ] Last reviewed date and reviewer name.

## Data sources currently planned (not yet integrated)

For traceability — these will land in P1+ and get full entries above when they do.

- **yfinance** — wrapper around Yahoo Finance public endpoints. Personal-use
  ToS only; requires re-evaluation before any commercial offering.
- **FRED** — Federal Reserve Economic Data API. Free, requires API key.
- **PRAW (Reddit)** — Reddit API for SNS sentiment. ToS imposes commercial-use
  restrictions; pseudonymous data is in scope of GDPR-equivalent regulations.
- **GDELT** — global news event dataset. Permissive license, attribution required.
- **X (Twitter)** — deferred. API costs and ToS restrictions are prohibitive
  for early prototyping.
