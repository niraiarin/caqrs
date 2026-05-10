"""Async Alpaca REST client for the live-broker submission path.

Per ADR-0009 §"Per-NFR mapping (NFR-LIVE-BROKER-4)" Alpaca paper
trading is the first venue. This client is a **thin httpx wrapper**
matching CAQRS's existing data-source style (see
``caqrs.data.edinet.client.EdinetClient`` for the precedent).

Credentials per ADR-0008 §NFR-LIVE-BROKER-2:

- ``LIVE_BROKER_API_KEY`` → ``APCA-API-KEY-ID`` header
- ``LIVE_BROKER_API_SECRET`` → ``APCA-API-SECRET-KEY`` header
- ``LIVE_BROKER_BASE_URL`` → endpoint root (defaults to the paper
  endpoint so a missing var keeps the broker safe)

The client reads only ``LIVE_BROKER_*`` env vars; the credential
isolation lint (PR #89) enforces this at the static-graph level.

Scope of this module: order submission + cancel-all + account fetch.
Trade-update websocket subscription is the **next** slice; not yet
implemented.
"""

from __future__ import annotations

import os
from decimal import Decimal
from types import TracebackType

import httpx

from caqrs.schemas.common import StrictBaseModel, Ticker
from caqrs.schemas.decision import Side

_DEFAULT_PAPER_BASE_URL = "https://paper-api.alpaca.markets"
_DEFAULT_TIMEOUT_SECONDS = 30.0


class AlpacaError(Exception):
    """All Alpaca REST failures the caller can match on.

    Wraps both 4xx/5xx envelopes and transport-level failures.
    Carries the venue HTTP status (when available) and the raw body
    so audit emission can record the venue's stated reason verbatim.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        venue_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.venue_body = venue_body


class AlpacaOrder(StrictBaseModel):
    """One submitted Alpaca order. Frozen, extra=forbid.

    ``order_id`` is the venue-assigned UUID Alpaca returns;
    ``client_order_id`` is the deterministic 48-char truncation of
    :meth:`LiveBrokerAlpaca.compute_idempotency_key`'s 64-char digest
    (per ADR-0009 §"Per-NFR mapping (NFR-LIVE-BROKER-4)") — both are
    persisted alongside the cycle event log so replay-vs-fresh-order
    disambiguation is recoverable post-hoc.
    """

    order_id: str
    client_order_id: str
    symbol: str
    qty: Decimal
    side: str
    status: str  # alpaca's order status: 'accepted' / 'pending_new' / 'rejected' / ...


class AlpacaRestClient:
    """Async-context-manager wrapper for Alpaca's REST API.

    Usage:

    .. code-block:: python

        async with AlpacaRestClient.from_env() as client:
            order = await client.submit_order(
                symbol="AAPL",
                qty=Decimal("10"),
                side=Side.BUY,
                client_order_id="<48-char-sha256-prefix>",
            )

    The constructor takes credentials + base URL explicitly (preferred
    in tests). :meth:`from_env` reads the canonical
    ``LIVE_BROKER_*`` env vars per ADR-0008 §NFR-LIVE-BROKER-2.
    """

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        base_url: str = _DEFAULT_PAPER_BASE_URL,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not api_key:
            msg = "api_key must be a non-empty string"
            raise ValueError(msg)
        if not api_secret:
            msg = "api_secret must be a non-empty string"
            raise ValueError(msg)
        self._api_key = api_key
        self._api_secret = api_secret
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "APCA-API-KEY-ID": api_key,
                "APCA-API-SECRET-KEY": api_secret,
                "Content-Type": "application/json",
            },
        )

    @classmethod
    def from_env(
        cls,
        *,
        api_key_var: str = "LIVE_BROKER_API_KEY",
        api_secret_var: str = "LIVE_BROKER_API_SECRET",
        base_url_var: str = "LIVE_BROKER_BASE_URL",
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> AlpacaRestClient:
        """Build a client from ``LIVE_BROKER_*`` env vars.

        ``base_url_var`` is optional: when unset the paper-trading URL
        is used, keeping the default off-the-shelf safe.
        """
        api_key = os.environ.get(api_key_var, "")
        if not api_key:
            msg = f"{api_key_var} env var must be set; export it (e.g. via dotenvx) and try again"
            raise AlpacaError(msg)
        api_secret = os.environ.get(api_secret_var, "")
        if not api_secret:
            msg = f"{api_secret_var} env var must be set"
            raise AlpacaError(msg)
        base_url = os.environ.get(base_url_var, _DEFAULT_PAPER_BASE_URL)
        return cls(
            api_key=api_key,
            api_secret=api_secret,
            base_url=base_url,
            timeout=timeout,
        )

    async def __aenter__(self) -> AlpacaRestClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self._client.aclose()

    async def submit_order(
        self,
        *,
        symbol: Ticker,
        qty: Decimal,
        side: Side,
        client_order_id: str,
        order_type: str = "market",
        time_in_force: str = "day",
    ) -> AlpacaOrder:
        """POST a market order to ``$BASE/v2/orders``.

        Step 1 placeholder: signature is in place so tests can target
        the final API; body is unimplemented until step 2.
        """
        raise NotImplementedError(
            "Alpaca REST step 1 placeholder; submit_order impl in step 2",
        )

    async def cancel_all_orders(self) -> None:
        """``DELETE /v2/orders`` — cancel every open order. Used by
        :meth:`LiveBrokerAlpaca.kill_switch` per ADR-0009.

        Step 1 placeholder."""
        raise NotImplementedError(
            "Alpaca REST step 1 placeholder; cancel_all_orders impl in step 2",
        )
