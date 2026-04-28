"""Async client for Polymarket Gamma public read-only endpoints.

Endpoints covered (all unauthenticated):

- ``GET /markets`` — list markets with rich filtering (slug,
  condition_ids, clob_token_ids, active/closed flags, liquidity /
  volume / date range). limit + offset pagination.
- ``GET /markets/{id}`` — fetch one market by numeric id.
- ``GET /markets/{slug}`` — fetch one market by slug (Polymarket
  routes both id and slug through the same path so the wrapper
  picks based on whether the argument is all-digits).

Quirks the client smooths over:

- ``clobTokenIds``, ``outcomes``, ``outcomePrices`` are JSON-encoded
  *strings* inside the JSON response, not arrays. We parse them so
  callers see plain tuples.
- Numeric fields (``volume``, ``liquidity``, ``lastTradePrice``)
  arrive as both strings and numbers; coerced to ``Decimal`` (or
  ``None`` for missing).
- Date fields are ISO-8601 strings; parsed to tz-aware
  :class:`datetime`.
- Boolean filters serialise to lowercase strings (``"true"`` /
  ``"false"``) on the wire.
- List filters (``slug``, ``clob_token_ids``, ``condition_ids``)
  are sent as repeated query parameters.
"""

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from types import TracebackType
from typing import Any, Self

import httpx

from caqrs.data._common.rate_limit import AsyncRateLimiter
from caqrs.data.polymarket.clob_client import (
    PolymarketError,
    _to_decimal,
)
from caqrs.data.polymarket.schemas import GammaMarket

_DEFAULT_BASE_URL = "https://gamma-api.polymarket.com"
_DEFAULT_TIMEOUT_S = 10.0
_HTTP_OK = 200
_HTTP_BAD_REQUEST_LIMIT = 500


class PolymarketGammaClient:
    """Async client for the Polymarket Gamma public endpoints.

    Lifecycle mirrors :class:`PolymarketClobClient`: enter the async
    context manager to own an httpx client, or pass an externally
    managed one to share connections across data sources.
    """

    def __init__(
        self,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        http_client: httpx.AsyncClient | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        rate_limiter: AsyncRateLimiter | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._owns_http_client = http_client is None
        self._http_client = http_client
        # Default no per-second pacing — public Gamma endpoints sit
        # comfortably below their rate budget for typical CAQRS use.
        # Callers batching across hundreds of slugs supply a paced
        # limiter (e.g. min_interval_seconds=0.01 for 100 req/s).
        self._rate_limiter = rate_limiter or AsyncRateLimiter(
            min_interval_seconds=0.0,
        )

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

    async def list_markets(
        self,
        *,
        limit: int | None = None,
        offset: int | None = None,
        order: str | None = None,
        ascending: bool | None = None,
        slugs: Sequence[str] | None = None,
        condition_ids: Sequence[str] | None = None,
        clob_token_ids: Sequence[str] | None = None,
        active: bool | None = None,
        closed: bool | None = None,
        liquidity_num_min: float | None = None,
        liquidity_num_max: float | None = None,
        volume_num_min: float | None = None,
        volume_num_max: float | None = None,
        end_date_min: datetime | None = None,
        end_date_max: datetime | None = None,
        tag_id: int | None = None,
    ) -> tuple[GammaMarket, ...]:
        """List markets with the supplied filters.

        Returns a tuple of :class:`GammaMarket`. The Gamma API
        defaults ``closed`` to ``False`` if unset; pass
        ``closed=False`` explicitly to include both open and closed
        markets.
        """
        params: list[tuple[str, str]] = []
        _add_optional(params, "limit", limit)
        _add_optional(params, "offset", offset)
        _add_optional(params, "order", order)
        _add_optional_bool(params, "ascending", ascending)
        _add_repeated(params, "slug", slugs)
        _add_repeated(params, "condition_ids", condition_ids)
        _add_repeated(params, "clob_token_ids", clob_token_ids)
        _add_optional_bool(params, "active", active)
        _add_optional_bool(params, "closed", closed)
        _add_optional(params, "liquidity_num_min", liquidity_num_min)
        _add_optional(params, "liquidity_num_max", liquidity_num_max)
        _add_optional(params, "volume_num_min", volume_num_min)
        _add_optional(params, "volume_num_max", volume_num_max)
        _add_optional_datetime(params, "end_date_min", end_date_min)
        _add_optional_datetime(params, "end_date_max", end_date_max)
        _add_optional(params, "tag_id", tag_id)

        body = await self._get_json("/markets", params=params)
        return _parse_market_list(body, endpoint="/markets")

    async def get_market(self, identifier: str) -> GammaMarket:
        """Fetch a single market by numeric id or slug.

        Polymarket's surface-level docs suggest ``GET /markets/{id}``
        accepts either, but the live API rejects slugs with 422
        ``"id is invalid"``. We dispatch:

        - All-digits identifier → ``GET /markets/{id}``.
        - Otherwise (slug) → ``GET /markets?slug=<slug>`` and take
          the singleton result.
        """
        if identifier.isdigit():
            return await self._get_market_by_id(identifier)
        return await self._get_market_by_slug(identifier)

    async def _get_market_by_id(self, market_id: str) -> GammaMarket:
        path = f"/markets/{market_id}"
        body = await self._get_json(path, params=[])
        if not isinstance(body, Mapping):
            msg = f"Polymarket gamma {path} returned non-object body"
            raise PolymarketError(message=msg)
        return _parse_market(body)

    async def _get_market_by_slug(self, slug: str) -> GammaMarket:
        body = await self._get_json("/markets", params=[("slug", slug)])
        markets = _parse_market_list(body, endpoint="/markets")
        if not markets:
            msg = f"Polymarket gamma slug {slug!r} not found"
            raise PolymarketError(message=msg, status_code=404)
        return markets[0]

    # === Internals ===

    async def _get_json(
        self,
        path: str,
        *,
        params: Sequence[tuple[str, str]],
    ) -> Any:
        client = self._http_client
        if client is None:
            async with httpx.AsyncClient(timeout=self._timeout_s) as one_shot:
                return await self._execute(one_shot, path, params=params)
        return await self._execute(client, path, params=params)

    async def _execute(
        self,
        client: httpx.AsyncClient,
        path: str,
        *,
        params: Sequence[tuple[str, str]],
    ) -> Any:
        url = f"{self._base_url}{path}"
        await self._rate_limiter.acquire()
        try:
            response = await client.get(url, params=list(params))
        except httpx.RequestError as exc:
            msg = f"Polymarket gamma request failed: {type(exc).__name__}: {exc}"
            raise PolymarketError(message=msg) from exc

        if response.status_code != _HTTP_OK:
            body = response.text[:_HTTP_BAD_REQUEST_LIMIT]
            msg = f"Polymarket gamma returned {response.status_code} for {path}: {body}"
            raise PolymarketError(message=msg, status_code=response.status_code)

        try:
            payload = response.json()
        except ValueError as exc:
            msg = f"Polymarket gamma returned non-JSON body for {path}: {exc}"
            raise PolymarketError(message=msg) from exc

        return payload


# === Query-param helpers ===


def _add_optional(
    params: list[tuple[str, str]],
    key: str,
    value: object,
) -> None:
    if value is None:
        return
    params.append((key, str(value)))


def _add_optional_bool(
    params: list[tuple[str, str]],
    key: str,
    value: bool | None,
) -> None:
    if value is None:
        return
    params.append((key, "true" if value else "false"))


def _add_optional_datetime(
    params: list[tuple[str, str]],
    key: str,
    value: datetime | None,
) -> None:
    if value is None:
        return
    params.append((key, value.isoformat()))


def _add_repeated(
    params: list[tuple[str, str]],
    key: str,
    values: Sequence[str] | None,
) -> None:
    if not values:
        return
    params.extend((key, v) for v in values)


# === Response parsers ===


def _parse_market_list(body: Any, *, endpoint: str) -> tuple[GammaMarket, ...]:
    """Parse a list of GammaMarket from a /markets response.

    Polymarket's /markets endpoint has historically returned both a
    bare array and an object wrapper (``{"data": [...]}``). The
    parser accepts either shape so the client survives that variance.
    """
    if isinstance(body, list):
        items = body
    elif isinstance(body, Mapping):
        data = body.get("data")
        if isinstance(data, list):
            items = data
        else:
            msg = (
                f"Polymarket gamma {endpoint} returned object body without 'data' array: "
                f"keys={list(body.keys())}"
            )
            raise PolymarketError(message=msg)
    else:
        msg = f"Polymarket gamma {endpoint} returned unexpected body type {type(body).__name__}"
        raise PolymarketError(message=msg)

    return tuple(_parse_market(item) for item in items)


def _parse_market(body: Mapping[str, Any]) -> GammaMarket:
    return GammaMarket(
        id=str(body.get("id", "")),
        question=_optional_str(body.get("question")),
        slug=_optional_str(body.get("slug")),
        end_date=_optional_iso_datetime(body.get("endDate"), field="endDate"),
        active=bool(body.get("active", False)),
        closed=bool(body.get("closed", False)),
        volume=_optional_decimal(body.get("volume"), field="volume"),
        liquidity=_optional_decimal(body.get("liquidity"), field="liquidity"),
        last_trade_price=_optional_decimal(body.get("lastTradePrice"), field="lastTradePrice"),
        clob_token_ids=_decode_string_list(body.get("clobTokenIds"), field="clobTokenIds"),
        outcomes=_decode_string_list(body.get("outcomes"), field="outcomes"),
        outcome_prices=_decode_decimal_list(body.get("outcomePrices"), field="outcomePrices"),
    )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _optional_decimal(value: Any, *, field: str) -> Decimal | None:
    if value is None or value == "":
        return None
    return _to_decimal(value, field=field)


def _optional_iso_datetime(value: Any, *, field: str) -> datetime | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        msg = f"Polymarket gamma field {field!r} is not a string ISO timestamp"
        raise PolymarketError(message=msg)
    try:
        # Polymarket emits both "Z" and "+00:00" suffixes; fromisoformat
        # accepts the latter natively, swap "Z" for compatibility.
        normalised = value.replace("Z", "+00:00") if value.endswith("Z") else value
        return datetime.fromisoformat(normalised)
    except ValueError as exc:
        msg = f"Polymarket gamma field {field!r} is not a valid ISO timestamp: {value!r}"
        raise PolymarketError(message=msg) from exc


def _decode_string_list(value: Any, *, field: str) -> tuple[str, ...]:
    """Polymarket encodes string arrays as JSON-stringified strings.
    Tolerate already-decoded arrays as well in case the upstream
    response shape changes."""
    if value is None or value == "":
        return ()
    if isinstance(value, list):
        return tuple(str(v) for v in value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            msg = f"Polymarket gamma field {field!r} is not a valid JSON-encoded list: {value!r}"
            raise PolymarketError(message=msg) from exc
        if not isinstance(decoded, list):
            msg = (
                f"Polymarket gamma field {field!r} decoded to "
                f"{type(decoded).__name__}, expected list"
            )
            raise PolymarketError(message=msg)
        return tuple(str(v) for v in decoded)
    msg = f"Polymarket gamma field {field!r} has unsupported type {type(value).__name__}"
    raise PolymarketError(message=msg)


def _decode_decimal_list(value: Any, *, field: str) -> tuple[Decimal, ...]:
    raw_strings = _decode_string_list(value, field=field)
    return tuple(_to_decimal(s, field=f"{field}[*]") for s in raw_strings)
