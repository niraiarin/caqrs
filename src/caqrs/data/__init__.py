"""External data sources for the Observer agent.

Each subpackage wraps a single source with an async client and typed
artifacts. The Observer composes whichever sources its current
:class:`ObserverInput` requires; CAQRS does not bundle a single
opinionated data layer because financial-research signals come from
many uncorrelated APIs (price feeds, news, social, prediction
markets, macro).

Conventions:

- All clients are read-only at this layer; trade execution lives
  behind the Policy Gateway (P3) and never touches these modules.
- Numeric prices/sizes are normalised to :class:`decimal.Decimal`
  even when the upstream API returns strings.
- Timestamps are timezone-aware UTC :class:`datetime`.
- Tests use ``respx`` to mock httpx; live smoke tests are gated by
  ``CAQRS_LIVE=1`` like the provider live tests.

P1.6.a — :mod:`caqrs.data.polymarket`: prediction-market signals
(implied probabilities, midpoints, price history) via the CLOB API.
"""
