# Data source license & ToS audit

This document tracks the legal status of every data source CAQRS ingests.
**It must be kept up to date** — `scripts/check_data_source_tos.py` runs in
CI and fails the build when a new package appears under
`src/caqrs/data/<source>/` without a corresponding row in the table below.

## Currently integrated

| Source              | Type                            | License / ToS                                                                                            | Allowed use                                                                | Rate limits                                                                          | Last reviewed |
| ------------------- | ------------------------------- | -------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ | ------------- |
| **edinet**          | Government API (JFSA)           | [EDINET API v2 terms](https://api.edinet-fsa.go.jp/) — public disclosure data, free Subscription-Key tier | Research, redistribution of derivative analyses with source attribution    | No published per-second cap; client uses default `AsyncRateLimiter` (no pacing)       | 2026-05-02    |
| **edinetdb**        | Third-party hosted API          | [edinetdb.jp Terms of Service](https://edinetdb.jp/) — `# review` confirm commercial-use clause          | Research; commercial use TBD `# review`                                    | Free plan: **100 req/day** (hard daily quota enforced via `DailyQuotaTracker`)        | 2026-05-02    |
| **jquants**         | Vendor API (JPX-official)       | [J-Quants Terms of Use](https://jpx-jquants.com/) — `# review` confirm exact ToS URL                     | Research on free tier; commercial / redistribution requires paid plan      | Free tier: **5 req/min** (12 s `min_interval_seconds`), 2-year history, 12-week delay | 2026-05-02    |
| **polymarket**      | Public REST API (CLOB + Gamma)  | [Polymarket Terms of Service](https://polymarket.com/tos) — `# review` confirm data-redistribution stance | Research; on-chain prediction-market data is publicly observable           | No published per-second cap; default limiter `min_interval_seconds=0.0` (unpaced)    | 2026-05-02    |
| **polymarket_archive** | Community parquet mirror     | [archive.pmxt.dev](https://archive.pmxt.dev/Polymarket/) — third-party mirror of public CLOB events `# review` | Research; mirror operator's terms TBD `# review`                          | No published cap; files are 100-400 MB hourly snapshots, fetched & cached locally     | 2026-05-02    |
| **yfinance**        | Library wrapper (Yahoo Finance) | [Yahoo Terms of Service](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html) — personal-use only  | Personal / research only; **no commercial use without separate license**   | No official limit; client treats 3 consecutive empty responses as quota exhaustion    | 2026-05-02    |

Rows tagged `# review` need a human ToS reviewer to confirm the link target
and clause interpretation before any commercial deployment. The lint
script does not validate URL contents — that is a human gate.

## Recently integrated

For traceability, mapping each table row to its CAQRS module and observer
helper:

- **edinet** — `caqrs.data.edinet` (`EdinetClient`,
  `fetch_recent_filings`). Wraps the JFSA EDINET v2 endpoints
  (`/documents.json`, `/documents/{docID}`) at
  `https://api.edinet-fsa.go.jp/api/v2/`. Free Subscription-Key tier.
- **edinetdb** — `caqrs.data.edinetdb` (`EdinetDbClient`,
  `EdinetDbCache`, `DailyQuotaTracker`,
  `fetch_edinetdb_company_fundamentals`). Hits
  `https://edinetdb.jp/v1/` for pre-parsed financials with a TTL SQLite
  cache and a persistent 100-req/day quota tracker.
- **jquants** — `caqrs.data.jquants` (`JQuantsClient`,
  `fetch_jquants_asset_snapshot`). Hits `https://api.jquants.com/v2/`
  with a 12-second `min_interval_seconds` to stay under the free 5
  req/min cap.
- **polymarket** — `caqrs.data.polymarket` (`PolymarketClobClient`,
  `PolymarketGammaClient`, `fetch_polymarket_signal`). Read-only against
  `https://clob.polymarket.com` and `https://gamma-api.polymarket.com`.
- **polymarket_archive** — `caqrs.data.polymarket_archive`
  (`PolymarketArchiveClient`, `load_events`). Hourly parquet snapshots
  from `https://r2v2.pmxt.dev`; ships behind the `archive` extra because
  `polars` is heavy.
- **yfinance** — `caqrs.data.yfinance` (`YFinanceClient`,
  `YFinanceCache`, `fetch_yfinance_asset_snapshot`). Async wrapper around
  the `yfinance` library with a per-process tz cache and empty-vs-rate-
  limited disambiguation.

## Review checklist

When adding a new data source, fill in:

- [ ] Source name and provider URL.
- [ ] License or ToS URL (link to the canonical document, with retrieval date).
- [ ] Allowed use (research / commercial / redistribution / derivative works).
- [ ] Rate limits and cost (free tier / paid tier).
- [ ] Required attribution and the exact attribution string.
- [ ] Personal data / GDPR considerations (PRAW exposes pseudonymous user data).
- [ ] Last reviewed date and reviewer name.

## Execution venues (informational)

Distinct from data sources above: these are brokerage / venue APIs CAQRS
submits orders to. The TOS lint (``scripts/check_data_source_tos.py``)
does NOT audit this section — its scope is ``src/caqrs/data/*`` ingestion
only — but commercial deployment MUST review each venue's customer
agreement before enabling live trading.

| Venue       | Type                         | License / ToS                                                                                              | Allowed use                                                                | Rate limits                                | Last reviewed |
| ----------- | ---------------------------- | ---------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- | ------------------------------------------ | ------------- |
| **alpaca**  | Brokerage API (paper + live) | [Alpaca Customer Agreement](https://alpaca.markets/legal) — `# review` confirm exact program terms          | Paper trading on free tier; live trading requires funded account + KYC      | Trading API: ~200 req/min per Alpaca docs | 2026-05-10    |

`# review` markers indicate clauses requiring human ToS reviewer
sign-off before commercial deployment. ADR-0009 selected Alpaca as the
first live-broker venue (paper-account first); ADR-0010 (forward
pointer) covers the next venue.

## Deferred (not yet integrated)

Forward-looking entries the lint accepts as "extras" — they document
sources we expect to integrate but haven't yet:

- **FRED** — Federal Reserve Economic Data API. Free, requires API key.
- **PRAW (Reddit)** — Reddit API for SNS sentiment. ToS imposes
  commercial-use restrictions; pseudonymous data is in scope of
  GDPR-equivalent regulations.
- **GDELT** — global news event dataset. Permissive license, attribution
  required.
- **X (Twitter)** — deferred. API costs and ToS restrictions are
  prohibitive for early prototyping.
