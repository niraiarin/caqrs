"""Async client for the J-Quants V2 API.

Wraps the ``x-api-key``-authenticated JSON HTTP API at
``https://api.jquants.com/v2``. Pagination follows
``pagination_key`` until the server omits it.

Free-tier endpoints currently exposed:

- ``GET /v2/equities/master`` (:meth:`JQuantsClient.list_master`)
- ``GET /v2/equities/bars/daily`` (:meth:`JQuantsClient.daily_bars`)

Earnings summary / calendar and the paid-tier endpoints follow in
subsequent slices once a caller actually needs them.
"""

from collections.abc import Mapping
from datetime import date as date_
from types import TracebackType
from typing import Any, Self

import httpx

from caqrs.data.jquants.schemas import JQuantsDailyBar, JQuantsListedStock

_DEFAULT_BASE_URL = "https://api.jquants.com/v2"
_DEFAULT_TIMEOUT_S = 30.0
_HTTP_OK = 200
_HTTP_BAD_REQUEST_LIMIT = 500


class JQuantsError(Exception):
    """Raised when a J-Quants request fails or returns an unparseable body."""

    def __init__(self, *, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class JQuantsClient:
    """Async client for J-Quants V2 endpoints.

    Authentication is a single API key passed in the ``x-api-key``
    header; pass it explicitly or set ``JQUANTS_API_KEY`` and read it
    in caller code.

    Parameters
    ----------
    api_key:
        The API key issued from the J-Quants dashboard. Empty string
        is rejected at construction time.
    base_url:
        Override the V2 endpoint, e.g. for a local proxy.
    http_client:
        Pre-built ``httpx.AsyncClient`` to share connections; the
        client will not close it on context exit.
    timeout_s:
        Per-request timeout. Free tier is 5 req/min so individual
        requests rarely take long; default 30 s is generous.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = _DEFAULT_BASE_URL,
        http_client: httpx.AsyncClient | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        if not api_key:
            msg = "api_key must be a non-empty string"
            raise ValueError(msg)
        self._api_key = api_key
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

    async def list_master(
        self,
        *,
        code: str | None = None,
        as_of: date_ | None = None,
    ) -> tuple[JQuantsListedStock, ...]:
        """Listed-stock master rows.

        Parameters
        ----------
        code:
            Optional 4 or 5-digit ticker code. When omitted the API
            returns the full universe (paginated).
        as_of:
            Optional reference date. Encoded as ``YYYYMMDD``.
        """
        params: dict[str, str] = {}
        if code is not None:
            params["code"] = code
        if as_of is not None:
            params["date"] = _yyyymmdd(as_of)
        rows = await self._get_paginated("/equities/master", params=params)
        return tuple(JQuantsListedStock.model_validate(row) for row in rows)

    async def daily_bars(
        self,
        *,
        code: str | None = None,
        as_of: date_ | None = None,
        from_date: date_ | None = None,
        to_date: date_ | None = None,
    ) -> tuple[JQuantsDailyBar, ...]:
        """Daily OHLCV bars.

        Either pass ``as_of`` (single-day snapshot) or ``from_date`` /
        ``to_date`` (range, both ``YYYYMMDD`` encoded). When ``as_of``
        is supplied it takes precedence — the upstream API rejects
        the combination, so we strip the range params before sending.
        """
        params: dict[str, str] = {}
        if code is not None:
            params["code"] = code
        if as_of is not None:
            params["date"] = _yyyymmdd(as_of)
        else:
            if from_date is not None:
                params["from"] = _yyyymmdd(from_date)
            if to_date is not None:
                params["to"] = _yyyymmdd(to_date)
        rows = await self._get_paginated("/equities/bars/daily", params=params)
        return tuple(JQuantsDailyBar.model_validate(row) for row in rows)

    # === Internals ===

    async def _get_paginated(
        self,
        path: str,
        *,
        params: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        """Follow ``pagination_key`` until the server omits it; concatenate ``data``."""
        url = f"{self._base_url}{path}"
        out: list[dict[str, Any]] = []
        query: dict[str, str] = dict(params)
        while True:
            payload = await self._get_json(url, params=query)
            batch = payload.get("data", [])
            if isinstance(batch, list):
                out.extend(batch)
            token = payload.get("pagination_key")
            if not token:
                return out
            query["pagination_key"] = str(token)

    async def _get_json(self, url: str, *, params: Mapping[str, str]) -> Mapping[str, Any]:
        client = self._http_client
        if client is None:
            async with httpx.AsyncClient(timeout=self._timeout_s) as one_shot:
                return await self._execute(one_shot, url, params=params)
        return await self._execute(client, url, params=params)

    async def _execute(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        params: Mapping[str, str],
    ) -> Mapping[str, Any]:
        try:
            response = await client.get(
                url,
                params=params,
                headers={"x-api-key": self._api_key},
            )
        except httpx.RequestError as exc:
            msg = f"J-Quants request failed: {type(exc).__name__}: {exc}"
            raise JQuantsError(message=msg) from exc

        if response.status_code != _HTTP_OK:
            body = response.text[:_HTTP_BAD_REQUEST_LIMIT]
            msg = f"J-Quants returned {response.status_code} for {url}: {body}"
            raise JQuantsError(message=msg, status_code=response.status_code)

        try:
            payload = response.json()
        except ValueError as exc:
            msg = f"J-Quants returned non-JSON body for {url}: {exc}"
            raise JQuantsError(message=msg) from exc

        if not isinstance(payload, Mapping):
            msg = f"J-Quants returned non-object body for {url}: {type(payload).__name__}"
            raise JQuantsError(message=msg)

        return payload


def _yyyymmdd(d: date_) -> str:
    return d.strftime("%Y%m%d")
