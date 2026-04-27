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

_BINARY_OUTCOME_COUNT = 2


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


class GammaMarket(StrictBaseModel):
    """Polymarket market metadata from the Gamma API.

    A Polymarket market resolves a single binary or multi-outcome
    question. ``outcomes`` and ``clob_token_ids`` are parallel
    arrays: ``outcomes[i]`` is the human-readable label and
    ``clob_token_ids[i]`` is the ERC-1155 token id whose price feeds
    the implied probability of that outcome resolving.

    Polymarket's Gamma API encodes these arrays as JSON-encoded
    strings inside the JSON response (so the wire format is e.g.
    ``"outcomes": "[\\"Yes\\", \\"No\\"]"``). The Gamma client decodes
    them before populating this model; downstream code sees plain
    tuples.
    """

    id: str = Field(min_length=1, description="Polymarket market id (numeric string).")
    question: str | None = None
    slug: str | None = None
    end_date: datetime | None = None
    active: bool = False
    closed: bool = False
    volume: Decimal | None = Field(default=None, description="USD volume (string-typed upstream).")
    liquidity: Decimal | None = Field(
        default=None,
        description="USD orderbook depth.",
    )
    last_trade_price: Probability | None = None
    clob_token_ids: tuple[str, ...] = ()
    outcomes: tuple[str, ...] = ()
    outcome_prices: tuple[Probability, ...] = ()

    @property
    def is_binary(self) -> bool:
        """True iff the market has exactly two outcomes (the common
        Yes/No shape). Multi-outcome markets (elections, etc.) need
        per-outcome lookup helpers instead of the convenience
        accessors below."""
        return len(self.outcomes) == _BINARY_OUTCOME_COUNT

    @property
    def yes_token_id(self) -> str | None:
        """Token id whose price = P(Yes). Only meaningful for binary
        markets; ``None`` otherwise."""
        return self._token_id_for("Yes")

    @property
    def no_token_id(self) -> str | None:
        return self._token_id_for("No")

    @property
    def yes_implied_prob(self) -> Probability | None:
        return self._price_for("Yes")

    @property
    def no_implied_prob(self) -> Probability | None:
        return self._price_for("No")

    def token_id_for_outcome(self, outcome: str) -> str | None:
        return self._token_id_for(outcome)

    def implied_prob_for_outcome(self, outcome: str) -> Probability | None:
        return self._price_for(outcome)

    def _token_id_for(self, outcome: str) -> str | None:
        if not self.is_binary:
            return None
        return self._lookup_parallel(self.clob_token_ids, outcome)

    def _price_for(self, outcome: str) -> Probability | None:
        if not self.is_binary:
            return None
        return self._lookup_parallel(self.outcome_prices, outcome)

    def _lookup_parallel[T](self, values: tuple[T, ...], outcome: str) -> T | None:
        target = outcome.casefold()
        for label, value in zip(self.outcomes, values, strict=False):
            if label.casefold() == target:
                return value
        return None
