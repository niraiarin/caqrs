"""EDINET DB (https://edinetdb.jp) — third-party hosted, structured-
financial-data API over JFSA EDINET filings.

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

from caqrs.data.edinetdb.client import (
    EdinetDbClient,
    EdinetDbError,
)
from caqrs.data.edinetdb.schemas import (
    EdinetDbCompaniesList,
    EdinetDbCompany,
    EdinetDbFinancialPeriod,
    EdinetDbPagination,
    EdinetDbRoeRanking,
)

__all__ = [
    "EdinetDbClient",
    "EdinetDbCompaniesList",
    "EdinetDbCompany",
    "EdinetDbError",
    "EdinetDbFinancialPeriod",
    "EdinetDbPagination",
    "EdinetDbRoeRanking",
]
