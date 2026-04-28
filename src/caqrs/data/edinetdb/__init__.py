"""EDINET DB (https://edinetdb.jp) — third-party hosted, structured-
financial-data API over JFSA EDINET filings.

⚠️  **Free plan rate limit: 100 requests / day** (as of 2026-04).
   This is a hard daily quota distinct from any per-second throttle.
   At the default 0.1 s throttle a careless caller exhausts the
   day's budget in ~10 seconds. Plan accordingly:

   - Cache aggressively. The `/companies` and `/financials` payloads
     are stable for days at a time; serve repeated reads from a
     local store (the yfinance `TTL SQLite cache` pattern in
     :mod:`caqrs.data.yfinance.cache` is the model to follow when a
     dedicated EDINET DB cache lands).
   - Prefetch in bulk before a screening loop, never fetch
     per-symbol inside a hot path.
   - Treat 429 as a hard stop until the next UTC day boundary.

This is **distinct** from :mod:`caqrs.data.edinet`, which targets the
official JFSA gateway at ``api.edinet-fsa.go.jp`` and serves raw
documents (XBRL zip / PDF / CSV). EDINET DB is a higher-level
service that pre-parses the same filings and exposes clean JSON
endpoints:

- ``GET /v1/companies?per_page=N&page=N`` — paginated company master.
- ``GET /v1/companies/{edinet_code}/financials`` — fiscal-year
  history (revenue, net income, BS / CF totals, EPS / BPS, etc.).
- ``GET /v1/rankings/roe?limit=N`` — ROE leaderboard with sec_code,
  industry, fiscal year, rank, value.

Auth is an ``X-API-Key`` HTTP header (NOT a query parameter, unlike
the official EDINET API). The key has the format ``edb_<32-char>``
and is read from the ``EDINETDB_API_KEY`` env var via
:meth:`EdinetDbClient.from_env`.

Choose between the two:

- Need raw XBRL / PDF / submission lists → ``caqrs.data.edinet``
- Need clean financial metrics (EPS / BPS / ROE) without parsing
  XBRL yourself → ``caqrs.data.edinetdb``
"""

from caqrs.data.edinetdb.cache import (
    DEFAULT_COMPANIES_TTL_SECONDS,
    DEFAULT_FINANCIALS_TTL_SECONDS,
    DEFAULT_RANKINGS_TTL_SECONDS,
    EdinetDbCache,
)
from caqrs.data.edinetdb.client import (
    EdinetDbClient,
    EdinetDbError,
)
from caqrs.data.edinetdb.observer_signals import (
    CompanyFundamentals,
    fetch_edinetdb_company_fundamentals,
)
from caqrs.data.edinetdb.quota import (
    DailyQuotaTracker,
    EdinetDbQuotaExhaustedError,
)
from caqrs.data.edinetdb.schemas import (
    EdinetDbCompaniesList,
    EdinetDbCompany,
    EdinetDbFinancialPeriod,
    EdinetDbMeta,
    EdinetDbPagination,
    EdinetDbRoeRanking,
)

__all__ = [
    "DEFAULT_COMPANIES_TTL_SECONDS",
    "DEFAULT_FINANCIALS_TTL_SECONDS",
    "DEFAULT_RANKINGS_TTL_SECONDS",
    "CompanyFundamentals",
    "DailyQuotaTracker",
    "EdinetDbCache",
    "EdinetDbClient",
    "EdinetDbCompaniesList",
    "EdinetDbCompany",
    "EdinetDbError",
    "EdinetDbFinancialPeriod",
    "EdinetDbMeta",
    "EdinetDbPagination",
    "EdinetDbQuotaExhaustedError",
    "EdinetDbRoeRanking",
    "fetch_edinetdb_company_fundamentals",
]
