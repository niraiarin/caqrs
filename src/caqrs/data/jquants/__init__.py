"""J-Quants data source — JPX-official Japan equities API.

JPX's J-Quants service exposes daily OHLCV, listed-stock master,
financials, and (on paid tiers) options / margin balance data via a
JSON HTTP API at ``https://api.jquants.com/v2``. Authentication is a
single API key issued from the dashboard, sent in the ``x-api-key``
header. Free tier is 5 req/min over a 2-year history with a 12-week
publication delay.

P1.11.a — :class:`JQuantsClient` covers the two free-tier endpoints
the Observer cares about most:

- ``GET /v2/equities/master`` — listed-stock master (sector / market
  / margin classification).
- ``GET /v2/equities/bars/daily`` — daily OHLCV with adjusted-price
  variants and morning-session breakdowns.

Both endpoints follow the J-Quants pagination convention
(``{"data": [...], "pagination_key": "..."}``); the client follows
``pagination_key`` until the server stops emitting it.

Earnings summary, calendar, and the paid-tier endpoints land in a
follow-up slice when callers need them.
"""

from caqrs.data.jquants.client import JQuantsClient, JQuantsError
from caqrs.data.jquants.schemas import JQuantsDailyBar, JQuantsListedStock

__all__ = [
    "JQuantsClient",
    "JQuantsDailyBar",
    "JQuantsError",
    "JQuantsListedStock",
]
