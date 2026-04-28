"""Async httpx client for EDINET DB (edinetdb.jp) v1.

Endpoints exposed in this slice (P1.13.a):

- :meth:`EdinetDbClient.list_companies` → ``GET /v1/companies``
- :meth:`EdinetDbClient.company_financials` →
  ``GET /v1/companies/{edinet_code}/financials``
- :meth:`EdinetDbClient.ranking_roe` →
  ``GET /v1/rankings/roe``

Auth: ``X-API-Key`` HTTP header (the EDINET DB key has the format
``edb_<32-char>``). Reads ``EDINETDB_API_KEY`` from env via
:meth:`from_env`.

Throttle: default 0.1s (10 req/sec). EDINET DB does not publish a
numeric rate limit, so the conservative interval matches the EDINET
official-API client; callers can override.
"""

from __future__ import annotations

import asyncio
import os
import time
from types import TracebackType
from typing import Any

import httpx

from caqrs.data.edinetdb.schemas import (
    EdinetDbCompaniesList,
    EdinetDbFinancialPeriod,
    EdinetDbRoeRanking,
)

_BASE_URL = "https://edinetdb.jp/v1"
_DEFAULT_THROTTLE_SECONDS = 0.1  # ~10 req/sec, conservative
_HTTP_OK = 200


class EdinetDbError(Exception):
    """All EDINET DB API failures the caller can match on."""


class EdinetDbClient:
    """Async-context-manager wrapper for EDINET DB v1.

    Usage:

    .. code-block:: python

        async with EdinetDbClient.from_env() as client:
            companies = await client.list_companies(per_page=100)
            financials = await client.company_financials(
                edinet_code="E02367",  # Toyota
            )
            roe_top10 = await client.ranking_roe(limit=10)
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        throttle_seconds: float = _DEFAULT_THROTTLE_SECONDS,
        timeout: float = 30.0,
        base_url: str = _BASE_URL,
    ) -> None:
        self._api_key = api_key
        self._throttle = throttle_seconds
        self._base_url = base_url
        self._client = httpx.AsyncClient(timeout=timeout)
        self._last_call_at: float = 0.0

    @classmethod
    def from_env(
        cls,
        *,
        env_var: str = "EDINETDB_API_KEY",
        throttle_seconds: float = _DEFAULT_THROTTLE_SECONDS,
    ) -> EdinetDbClient:
        key = os.environ.get(env_var, "").strip()
        if not key:
            msg = (
                f"{env_var} is not set. Issue an X-API-Key from the "
                "EDINET DB dashboard at https://edinetdb.jp/ and "
                "export it (e.g. via dotenvx)."
            )
            raise EdinetDbError(msg)
        return cls(api_key=key, throttle_seconds=throttle_seconds)

    @property
    def api_key(self) -> str | None:
        return self._api_key

    async def __aenter__(self) -> EdinetDbClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # === Public API ===

    async def list_companies(
        self,
        *,
        page: int = 1,
        per_page: int = 20,
    ) -> EdinetDbCompaniesList:
        """Paginated company master."""
        params = {"page": str(page), "per_page": str(per_page)}
        body = await self._get_json(f"{self._base_url}/companies", params=params)
        return EdinetDbCompaniesList.model_validate(body, strict=False)

    async def company_financials(
        self,
        *,
        edinet_code: str,
    ) -> tuple[EdinetDbFinancialPeriod, ...]:
        """All available fiscal-year rows for the given ``edinet_code``.

        Order is upstream-decided (typically chronological); callers
        sort by ``fiscal_year`` if a specific order matters.
        """
        body = await self._get_json(
            f"{self._base_url}/companies/{edinet_code}/financials",
            params={},
        )
        rows = body.get("data", [])
        return tuple(EdinetDbFinancialPeriod.model_validate(row, strict=False) for row in rows)

    async def ranking_roe(
        self,
        *,
        limit: int = 10,
    ) -> tuple[EdinetDbRoeRanking, ...]:
        """Top-``limit`` companies by latest-fiscal-year ROE."""
        body = await self._get_json(
            f"{self._base_url}/rankings/roe",
            params={"limit": str(limit)},
        )
        rows = body.get("data", [])
        return tuple(EdinetDbRoeRanking.model_validate(row, strict=False) for row in rows)

    # === Helpers ===

    def _require_api_key(self) -> str:
        if not self._api_key:
            msg = (
                "EdinetDbClient was constructed without an api_key; "
                "EDINET DB v1 requires an X-API-Key header on every "
                "request. Pass api_key=... or use EdinetDbClient.from_env()."
            )
            raise EdinetDbError(msg)
        return self._api_key

    async def _get_json(
        self,
        url: str,
        *,
        params: dict[str, str],
    ) -> dict[str, Any]:
        await self._throttle_sleep()
        try:
            resp = await self._client.get(
                url,
                params=params,
                headers={"X-API-Key": self._require_api_key()},
            )
        except httpx.HTTPError as exc:
            msg = f"EDINET DB transport error: {exc}"
            raise EdinetDbError(msg) from exc

        if resp.status_code != _HTTP_OK:
            self._raise_from_error_response(resp)

        body: dict[str, Any] = resp.json()
        return body

    @staticmethod
    def _raise_from_error_response(resp: httpx.Response) -> None:
        """Surface EDINET DB error envelopes as ``EdinetDbError``.

        Tries the JSON body first (the API returns ``{statusCode,
        message}`` or ``{message}`` on errors); falls back to the raw
        text if the body isn't parseable JSON.
        """
        try:
            body = resp.json()
        except ValueError:
            msg = f"EDINET DB HTTP {resp.status_code}: {resp.text[:200]}"
            raise EdinetDbError(msg) from None

        # Live API uses camelCase ``statusCode`` (not the spec'd
        # PascalCase ``StatusCode``); accept both like the EDINET
        # official client does.
        status = body.get("statusCode") or body.get("StatusCode") or resp.status_code
        message = body.get("message", "")
        msg = f"EDINET DB {status}: {message}"
        raise EdinetDbError(msg)

    async def _throttle_sleep(self) -> None:
        if self._throttle <= 0:
            return
        elapsed = time.monotonic() - self._last_call_at
        wait = self._throttle - elapsed
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call_at = time.monotonic()
