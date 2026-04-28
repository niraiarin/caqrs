"""yFinance data source — Yahoo Finance daily bars + financials.

The ``yfinance`` library scrapes Yahoo Finance and presents a sync
API. CAQRS wraps it in three production safeguards documented in the
Zenn yfinance-production-pitfalls article:

1. **Async-first** — every call goes through ``asyncio.to_thread`` so
   the cycle's event loop is never blocked.
2. **Per-process tz cache** — ``~/.cache/py-yfinance`` SQLite has
   write-contention bugs under concurrent processes; the client uses
   a fresh ``tempfile.mkdtemp`` per instance and cleans up on
   ``aclose``.
3. **Empty-vs-rate-limited disambiguation** — yfinance silently
   returns empty DataFrames in both "no data" and "rate-limited"
   cases. The client treats 3 consecutive empties as a quota
   exhaustion signal and raises :class:`YFinanceQuotaExhaustedError`,
   which callers can catch + back off.

Default install does not pull yfinance; this subpackage requires the
``yfinance`` optional dependency:

.. code-block:: shell

    pip install caqrs[yfinance]
"""

from caqrs.data.yfinance.cache import (
    DEFAULT_FINANCIALS_TTL_SECONDS,
    DEFAULT_PRICES_TTL_SECONDS,
    YFinanceCache,
)
from caqrs.data.yfinance.client import (
    YFinanceClient,
    YFinanceError,
    YFinanceQuotaExhaustedError,
)
from caqrs.data.yfinance.observer_signals import fetch_yfinance_asset_snapshot
from caqrs.data.yfinance.schemas import (
    YFinanceFinancialPeriod,
    YFinancePrice,
)

__all__ = [
    "DEFAULT_FINANCIALS_TTL_SECONDS",
    "DEFAULT_PRICES_TTL_SECONDS",
    "YFinanceCache",
    "YFinanceClient",
    "YFinanceError",
    "YFinanceFinancialPeriod",
    "YFinancePrice",
    "YFinanceQuotaExhaustedError",
    "fetch_yfinance_asset_snapshot",
]
