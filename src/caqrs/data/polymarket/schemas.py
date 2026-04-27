"""Typed artifacts for Polymarket CLOB responses.

Polymarket returns prices and sizes as strings on most endpoints
(e.g. orderbook ``"price": "0.45"``) but as numbers on others
(``GET /price`` returns ``{"price": 0.45}``). The schemas here
normalise to :class:`decimal.Decimal`; the client is responsible
for the string→Decimal coercion before constructing these models.

Probabilities live in ``[0, 1]`` because Polymarket's binary
contracts settle to $1 (YES) or $0 (NO); the price is the implied
probability of resolution. Sizes are in YES/NO shares (integer-like
but expressed as Decimal because the API may quote fractional
amounts after fee adjustments).
"""

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import Field

from caqrs.schemas.common import StrictBaseModel

Probability = Annotated[
    Decimal,
    Field(ge=0, le=1, description="Implied probability in [0, 1]; equals contract price."),
]


class OrderbookLevel(StrictBaseModel):
    """One price level on a side of the book."""

    price: Probability
    size: Annotated[Decimal, Field(ge=0)]


class Orderbook(StrictBaseModel):
    """Full orderbook snapshot for a single token (one side of a binary market).

    Polymarket markets have *two* tokens — one for YES and one for
    NO. Each has its own orderbook keyed by ``asset_id``.
    """

    market: str = Field(min_length=1, description="Polymarket condition id (hex string).")
    asset_id: str = Field(min_length=1, description="Token id (hex string).")
    timestamp: datetime
    bids: tuple[OrderbookLevel, ...]
    asks: tuple[OrderbookLevel, ...]
    min_order_size: Annotated[Decimal, Field(ge=0)]
    tick_size: Annotated[Decimal, Field(gt=0)]
    neg_risk: bool
    last_trade_price: Probability | None = None

    @property
    def best_bid(self) -> Probability | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Probability | None:
        return self.asks[0].price if self.asks else None

    @property
    def midpoint(self) -> Probability | None:
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2

    @property
    def spread(self) -> Decimal | None:
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None:
            return None
        return ask - bid


class PricePoint(StrictBaseModel):
    """One ``(timestamp, price)`` sample from the price-history endpoint."""

    timestamp: datetime
    price: Probability


class PriceHistory(StrictBaseModel):
    """Sequence of price samples for a single token over a window."""

    asset_id: str = Field(min_length=1)
    interval: str = Field(min_length=1)
    points: tuple[PricePoint, ...]

    @property
    def latest(self) -> PricePoint | None:
        return self.points[-1] if self.points else None

    @property
    def earliest(self) -> PricePoint | None:
        return self.points[0] if self.points else None
