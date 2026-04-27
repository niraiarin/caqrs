"""Async client for Polymarket CLOB public read-only endpoints.

Endpoints covered (all unauthenticated):

- ``GET /midpoint`` — average of best bid + best ask.
- ``GET /price`` — best price for a given side (BUY / SELL).
- ``GET /book`` — full orderbook snapshot.
- ``GET /prices-history`` — historical price samples for a window.

Quirks the client smooths over:

- Prices and sizes are returned as both strings (``"0.45"``) and
  numbers (``0.45``) depending on the endpoint. The client coerces
  everything to :class:`decimal.Decimal`.
- ``GET /prices-history`` uses the query parameter ``market`` even
  though the value is a token id (the same value other endpoints
  call ``token_id``). The client takes ``token_id`` everywhere and
  rewrites it on the wire.
- The orderbook ``timestamp`` field is a unix-seconds string;
  decoded to tz-aware UTC :class:`datetime`.

Rate-limit policy: caller's responsibility for now. Polymarket
publishes generous public-endpoint limits (~1.5k req / 10s for the
market-data routes per `docs <https://docs.polymarket.com/api-reference/rate-limits>`_)
so a single Observer cycle will not approach them. If a future caller
batches across hundreds of markets we will add explicit pacing here.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import TracebackType
from typing import Any, Self

import httpx

from caqrs.data.polymarket.schemas import (
    Orderbook,
    OrderbookLevel,
    PriceHistory,
    PricePoint,
)

_DEFAULT_BASE_URL = "https://clob.polymarket.com"
_DEFAULT_TIMEOUT_S = 10.0
_HTTP_OK = 200
_HTTP_BAD_REQUEST_LIMIT = 500


class Side(StrEnum):
    """Side argument for ``GET /price``."""

    BUY = "BUY"
    SELL = "SELL"


class PriceHistoryInterval(StrEnum):
    """Supported intervals for ``GET /prices-history``.

    Mirrors the values listed in the docs:
    https://docs.polymarket.com/api-reference/markets/get-prices-history
    """

    ONE_MINUTE = "1m"
    ONE_HOUR = "1h"
    SIX_HOURS = "6h"
    ONE_DAY = "1d"
    ONE_WEEK = "1w"
    MAX = "max"
    ALL = "all"


class PolymarketError(Exception):
    """Raised when a CLOB request fails or returns an unparseable body."""

    def __init__(self, *, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class PolymarketClobClient:
    """Async client for the Polymarket CLOB public endpoints.

    Usable two ways:

    1. As an async context manager — owns and closes its own httpx
       client::

           async with PolymarketClobClient() as clob:
               mid = await clob.get_midpoint(token_id="...")

    2. With an externally managed httpx client (caller controls
       lifecycle, useful for sharing connections across multiple
       data sources)::

           async with httpx.AsyncClient() as http:
               clob = PolymarketClobClient(http_client=http)
               ...
    """

    def __init__(
        self,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        http_client: httpx.AsyncClient | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._owns_http_client = http_client is None
        self._http_client = http_client

    async def __aenter__(self) -> Self:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=self._timeout_s)
            self._owns_http_client = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    # === Public methods ===

    async def get_midpoint(self, *, token_id: str) -> Decimal:
        """Best-bid / best-ask midpoint for the given token id.

        The CLOB returns ``{"mid_price": "0.45"}`` (string).
        """
        body = await self._get_json("/midpoint", params={"token_id": token_id})
        return _coerce_decimal(body, "mid_price")

    async def get_price(self, *, token_id: str, side: Side) -> Decimal:
        """Best price on the requested side for the given token id.

        The CLOB returns ``{"price": 0.45}`` (number) for this
        endpoint.
        """
        body = await self._get_json(
            "/price",
            params={"token_id": token_id, "side": side.value},
        )
        return _coerce_decimal(body, "price")

    async def get_orderbook(self, *, token_id: str) -> Orderbook:
        """Full orderbook snapshot."""
        body = await self._get_json("/book", params={"token_id": token_id})
        return _parse_orderbook(body)

    async def get_price_history(
        self,
        *,
        token_id: str,
        interval: PriceHistoryInterval = PriceHistoryInterval.ONE_DAY,
        fidelity_minutes: int | None = None,
        start_ts: int | None = None,
        end_ts: int | None = None,
    ) -> PriceHistory:
        """Historical (timestamp, price) samples for the token.

        Note Polymarket uses ``market`` (not ``token_id``) as the
        query parameter on this endpoint even though the value is a
        token / asset id. The client takes ``token_id`` everywhere
        and rewrites it on the wire.
        """
        params: dict[str, str | int] = {
            "market": token_id,
            "interval": interval.value,
        }
        if fidelity_minutes is not None:
            params["fidelity"] = fidelity_minutes
        if start_ts is not None:
            params["startTs"] = start_ts
        if end_ts is not None:
            params["endTs"] = end_ts
        body = await self._get_json("/prices-history", params=params)
        return _parse_price_history(body, asset_id=token_id, interval=interval.value)

    # === Internals ===

    async def _get_json(
        self,
        path: str,
        *,
        params: Mapping[str, str | int],
    ) -> Mapping[str, Any]:
        client = self._http_client
        if client is None:
            # Implicit one-shot client when caller did not enter the
            # async context manager. Close it immediately after.
            async with httpx.AsyncClient(timeout=self._timeout_s) as one_shot:
                return await self._execute(one_shot, path, params=params)
        return await self._execute(client, path, params=params)

    async def _execute(
        self,
        client: httpx.AsyncClient,
        path: str,
        *,
        params: Mapping[str, str | int],
    ) -> Mapping[str, Any]:
        url = f"{self._base_url}{path}"
        try:
            response = await client.get(url, params=params)
        except httpx.RequestError as exc:
            msg = f"Polymarket request failed: {type(exc).__name__}: {exc}"
            raise PolymarketError(message=msg) from exc

        if response.status_code != _HTTP_OK:
            body = response.text[:_HTTP_BAD_REQUEST_LIMIT]
            msg = f"Polymarket returned {response.status_code} for {path}: {body}"
            raise PolymarketError(message=msg, status_code=response.status_code)

        try:
            payload = response.json()
        except ValueError as exc:
            msg = f"Polymarket returned non-JSON body for {path}: {exc}"
            raise PolymarketError(message=msg) from exc

        if not isinstance(payload, Mapping):
            msg = f"Polymarket returned non-object body for {path}: {type(payload).__name__}"
            raise PolymarketError(message=msg)

        return payload


# === Parsers ===


def _coerce_decimal(body: Mapping[str, Any], key: str) -> Decimal:
    if key not in body:
        msg = f"Polymarket response missing key {key!r}: {dict(body)}"
        raise PolymarketError(message=msg)
    value = body[key]
    return _to_decimal(value, field=key)


def _to_decimal(value: Any, *, field: str) -> Decimal:
    """Coerce a Polymarket numeric field to Decimal regardless of int/float/str shape."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # bool is a subclass of int — guard explicitly
        msg = f"Polymarket field {field!r} is a bool, expected numeric"
        raise PolymarketError(message=msg)
    if isinstance(value, int | float):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation as exc:
            msg = f"Polymarket field {field!r} is not a valid number: {value!r}"
            raise PolymarketError(message=msg) from exc
    msg = f"Polymarket field {field!r} has unsupported type {type(value).__name__}"
    raise PolymarketError(message=msg)


def _to_unix_seconds_datetime(value: Any, *, field: str) -> datetime:
    """Polymarket sometimes returns a unix-seconds value as a string, sometimes as int.
    Normalise to tz-aware UTC datetime.
    """
    if isinstance(value, str):
        try:
            seconds = int(value)
        except ValueError as exc:
            msg = f"Polymarket field {field!r} is not an integer-string timestamp: {value!r}"
            raise PolymarketError(message=msg) from exc
    elif isinstance(value, int) and not isinstance(value, bool):
        seconds = value
    else:
        msg = f"Polymarket field {field!r} has unsupported timestamp type {type(value).__name__}"
        raise PolymarketError(message=msg)
    return datetime.fromtimestamp(seconds, tz=UTC)


def _parse_orderbook_levels(raw: Any, *, side: str) -> tuple[OrderbookLevel, ...]:
    if not isinstance(raw, list):
        msg = f"Polymarket orderbook {side!r} is not a list: {type(raw).__name__}"
        raise PolymarketError(message=msg)
    levels: list[OrderbookLevel] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            msg = f"Polymarket orderbook {side!r}[{i}] is not a mapping"
            raise PolymarketError(message=msg)
        levels.append(
            OrderbookLevel(
                price=_to_decimal(entry.get("price"), field=f"{side}[{i}].price"),
                size=_to_decimal(entry.get("size"), field=f"{side}[{i}].size"),
            ),
        )
    return tuple(levels)


def _parse_orderbook(body: Mapping[str, Any]) -> Orderbook:
    last_trade_raw = body.get("last_trade_price")
    last_trade = _to_decimal(last_trade_raw, field="last_trade_price") if last_trade_raw else None
    return Orderbook(
        market=str(body.get("market", "")),
        asset_id=str(body.get("asset_id", "")),
        timestamp=_to_unix_seconds_datetime(body.get("timestamp"), field="timestamp"),
        bids=_parse_orderbook_levels(body.get("bids", []), side="bids"),
        asks=_parse_orderbook_levels(body.get("asks", []), side="asks"),
        min_order_size=_to_decimal(body.get("min_order_size"), field="min_order_size"),
        tick_size=_to_decimal(body.get("tick_size"), field="tick_size"),
        neg_risk=bool(body.get("neg_risk", False)),
        last_trade_price=last_trade,
    )


def _parse_price_history(
    body: Mapping[str, Any],
    *,
    asset_id: str,
    interval: str,
) -> PriceHistory:
    raw_history = body.get("history", [])
    if not isinstance(raw_history, list):
        msg = f"Polymarket prices-history 'history' is not a list: {type(raw_history).__name__}"
        raise PolymarketError(message=msg)
    points: list[PricePoint] = []
    for i, entry in enumerate(raw_history):
        if not isinstance(entry, Mapping):
            msg = f"Polymarket prices-history history[{i}] is not a mapping"
            raise PolymarketError(message=msg)
        points.append(
            PricePoint(
                timestamp=_to_unix_seconds_datetime(entry.get("t"), field=f"history[{i}].t"),
                price=_to_decimal(entry.get("p"), field=f"history[{i}].p"),
            ),
        )
    return PriceHistory(asset_id=asset_id, interval=interval, points=tuple(points))
