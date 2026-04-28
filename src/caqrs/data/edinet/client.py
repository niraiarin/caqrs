"""Async httpx client for EDINET API v2.

Implements both endpoints documented in `ESE140206.pdf`:

- ``GET /api/v2/documents.json`` — daily submission list.
- ``GET /api/v2/documents/{docID}`` — document body (XBRL zip / PDF /
  attachments / English / CSV).

Auth: ``Subscription-Key`` is a **query parameter** on every call (per
spec section 3-1-1 / 3-2-1). The client reads it from the constructor
or from the ``EDINET_API_KEY`` env var via :meth:`from_env`.

Error semantics: the upstream gateway always returns HTTP 200; logical
errors are signalled via ``metadata.status`` ≠ "200" in the JSON body
(spec 3-3 example A) or, for invalid keys, a flat
``{StatusCode, message}`` shape (spec 3-3 example B). Both shapes
surface as :class:`EdinetError`. For document downloads, the response
``Content-Type`` distinguishes ZIP / PDF / error-JSON.

Throttle: ``throttle_seconds`` (default 0.1s, ~10 req/sec) keeps the
client below the unofficial conservative cap. EDINET's spec does not
publish a numeric rate limit, so the conservative interval is the
safest default for unattended supervisors.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import date as _date
from types import TracebackType
from typing import Any

import httpx

from caqrs.data.edinet.schemas import EdinetDocumentsList

_BASE_URL = "https://api.edinet-fsa.go.jp/api/v2"
_DEFAULT_THROTTLE_SECONDS = 0.1  # ~10 req/sec, conservative
_VALID_DOC_TYPES: frozenset[int] = frozenset({1, 2, 3, 4, 5})
_HTTP_OK = 200


class EdinetError(Exception):
    """All EDINET API failures the caller can match on.

    Wraps both kinds of EDINET error envelope (metadata.status ≠ 200,
    and the flat 401 ``{StatusCode, message}`` payload), plus
    transport-level failures.
    """


class EdinetClient:
    """Async-context-manager wrapper for the EDINET v2 REST API.

    Usage:

    .. code-block:: python

        async with EdinetClient.from_env() as client:
            listing = await client.documents_list(date_=date.today())
            zip_bytes = await client.download_document(
                doc_id=listing.results[0].doc_id,
                doc_type=1,
            )
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
        env_var: str = "EDINET_API_KEY",
        throttle_seconds: float = _DEFAULT_THROTTLE_SECONDS,
    ) -> EdinetClient:
        """Build a client whose ``api_key`` is read from ``$EDINET_API_KEY``.

        Raises :class:`EdinetError` if the env var is unset or empty
        — silent zero-key would 401 on every call, which is loud
        enough but slower to diagnose.
        """
        key = os.environ.get(env_var, "").strip()
        if not key:
            msg = (
                f"{env_var} is not set. Issue a Subscription-Key from "
                "https://disclosure2.edinet-fsa.go.jp/ (free tier) and "
                "export it (e.g. via dotenvx)."
            )
            raise EdinetError(msg)
        return cls(api_key=key, throttle_seconds=throttle_seconds)

    @property
    def api_key(self) -> str | None:
        """The configured Subscription-Key. ``None`` when the client
        was constructed without a key (calls will raise on first use)."""
        return self._api_key

    async def __aenter__(self) -> EdinetClient:
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

    async def documents_list(
        self,
        *,
        date_: _date,
        include_results: bool = True,
    ) -> EdinetDocumentsList:
        """Fetch the documents-list response for ``date_``.

        ``include_results=True`` (default) maps to ``type=2`` —
        metadata + full submission list. ``include_results=False``
        maps to ``type=1`` — metadata only, useful for cheap
        "is anything new" polls (spec 3-1-2-1 vs 3-1-2-2).
        """
        params: dict[str, Any] = {
            "date": date_.isoformat(),
            "type": "2" if include_results else "1",
            "Subscription-Key": self._require_api_key(),
        }
        body = await self._get_json(f"{self._base_url}/documents.json", params=params)
        self._raise_on_logical_error(body)
        # JSON has no tuple type, so the HTTP boundary uses strict=False
        # for list→tuple + str→bool coercion. The Pydantic-internal
        # cycle pipeline still uses strict=True for type safety.
        return EdinetDocumentsList.model_validate(body, strict=False)

    async def download_document(
        self,
        *,
        doc_id: str,
        doc_type: int,
    ) -> bytes:
        """Fetch the body of a single document.

        ``doc_type`` selects the file kind per spec 3-2-1:
        1 = XBRL zip (本文 + 監査), 2 = PDF, 3 = 添付文書 zip,
        4 = 英文ファイル zip, 5 = CSV zip.
        """
        if doc_type not in _VALID_DOC_TYPES:
            msg = f"doc_type must be one of {sorted(_VALID_DOC_TYPES)}; got {doc_type}"
            raise ValueError(msg)
        params: dict[str, Any] = {
            "type": str(doc_type),
            "Subscription-Key": self._require_api_key(),
        }
        await self._throttle_sleep()
        try:
            resp = await self._client.get(f"{self._base_url}/documents/{doc_id}", params=params)
        except httpx.HTTPError as exc:
            msg = f"EDINET transport error: {exc}"
            raise EdinetError(msg) from exc

        # Per spec 3-3 callout: download endpoint signals errors via
        # Content-Type=application/json (the binary endpoints use
        # application/octet-stream or application/pdf).
        content_type = resp.headers.get("content-type", "")
        if content_type.startswith("application/json"):
            body = resp.json()
            self._raise_on_logical_error(body)
            # If status was "200" but Content-Type was JSON, that's
            # itself an upstream protocol bug; surface it.
            msg = (
                f"EDINET document download returned application/json "
                f"with status=200 — unexpected for doc_id={doc_id}"
            )
            raise EdinetError(msg)
        return resp.content

    # === Helpers ===

    def _require_api_key(self) -> str:
        if not self._api_key:
            msg = (
                "EdinetClient was constructed without an api_key; "
                "EDINET v2 requires a Subscription-Key on every "
                "request. Pass api_key=... or use EdinetClient.from_env()."
            )
            raise EdinetError(msg)
        return self._api_key

    async def _get_json(self, url: str, *, params: dict[str, Any]) -> dict[str, Any]:
        await self._throttle_sleep()
        try:
            resp = await self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            msg = f"EDINET transport error: {exc}"
            raise EdinetError(msg) from exc

        if resp.status_code != _HTTP_OK:
            msg = f"EDINET HTTP {resp.status_code}: {resp.text[:200]}"
            raise EdinetError(msg)
        body: dict[str, Any] = resp.json()
        return body

    @staticmethod
    def _raise_on_logical_error(body: dict[str, Any]) -> None:
        """Detect the two error envelopes documented in spec 3-3.

        - metadata.status != "200" (the standard wrapper).
        - top-level ``{StatusCode | statusCode, message}`` for invalid
          API keys.

        Spec 3-3 example B documents ``StatusCode`` (PascalCase) but
        the live gateway also returns ``statusCode`` (camelCase). We
        accept both — silent drift here would let the body fall through
        to schema validation and surface as a confusing pydantic error.
        """
        # Flat 401 shape (spec 3-3 example B). Accept both PascalCase
        # and camelCase status keys.
        for status_key in ("StatusCode", "statusCode"):
            if status_key in body and "message" in body:
                status = body.get(status_key)
                message = body.get("message", "")
                msg = f"EDINET {status}: {message}"
                raise EdinetError(msg)
        # Standard metadata-wrapped errors (spec 3-3 example A).
        metadata = body.get("metadata", {})
        status = metadata.get("status")
        if status is not None and status != "200":
            message = metadata.get("message", "")
            msg = f"EDINET {status}: {message}"
            raise EdinetError(msg)

    async def _throttle_sleep(self) -> None:
        if self._throttle <= 0:
            return
        elapsed = time.monotonic() - self._last_call_at
        wait = self._throttle - elapsed
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call_at = time.monotonic()
