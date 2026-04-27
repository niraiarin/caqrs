"""Polymarket data source.

Implied probabilities from prediction markets are a low-latency
sentiment / regime signal: a market priced at 0.72 reflects the
crowd's pooled belief that the binary event will resolve YES, with
real money behind every bid.

P1.6.a — :class:`PolymarketClobClient` covers the public read-only
endpoints (``/midpoint``, ``/price``, ``/book``, ``/prices-history``)
of the CLOB API. No authentication required; all data is public.

Trade history (Data API) and order placement (CLOB authenticated
endpoints) are out of scope for the Observer's data-gathering role.
"""

from caqrs.data.polymarket.clob_client import (
    PolymarketClobClient,
    PolymarketError,
    PriceHistoryInterval,
    Side,
)
from caqrs.data.polymarket.schemas import (
    Orderbook,
    OrderbookLevel,
    PriceHistory,
    PricePoint,
)

__all__ = [
    "Orderbook",
    "OrderbookLevel",
    "PolymarketClobClient",
    "PolymarketError",
    "PriceHistory",
    "PriceHistoryInterval",
    "PricePoint",
    "Side",
]
