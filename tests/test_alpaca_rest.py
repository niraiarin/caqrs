"""Unit tests for the Alpaca REST client.

ADR-0006 step 1 / step 2 dispatch:

- **Step 1** (this commit): every test below is decorated
  ``@pytest.mark.xfail(strict=True, reason="impl pending — Alpaca REST")``
  and exercises the public surface of
  :mod:`caqrs.execution.alpaca_rest`. The client methods raise
  ``NotImplementedError`` so all tests xfail cleanly.
- **Step 2** (next commit): bodies implemented; xfail markers
  removed in the same commit that turns the assertions green.

Tests use ``respx`` to mock Alpaca's REST endpoints — the same
pattern as the existing data-source tests
(``tests/test_polymarket_*.py``).
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
import respx

from caqrs.execution.alpaca_rest import (
    AlpacaError,
    AlpacaRestClient,
)
from caqrs.schemas.decision import Side

_PAPER_BASE_URL = "https://paper-api.alpaca.markets"


@pytest.mark.asyncio
async def test_submit_order_returns_parsed_alpaca_order_on_success() -> None:
    """A successful POST /v2/orders MUST parse the JSON body into an
    :class:`AlpacaOrder` with the venue-assigned id, the
    client_order_id we sent, and the venue's reported status
    (typically 'accepted' or 'pending_new')."""
    with respx.mock(base_url=_PAPER_BASE_URL) as router:
        router.post("/v2/orders").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "abc123-uuid",
                    "client_order_id": "abcdef0123456789",
                    "symbol": "AAPL",
                    "qty": "10",
                    "side": "buy",
                    "status": "accepted",
                },
            ),
        )
        async with AlpacaRestClient(api_key="k", api_secret="s") as client:
            order = await client.submit_order(
                symbol="AAPL",
                qty=Decimal("10"),
                side=Side.BUY,
                client_order_id="abcdef0123456789",
            )
    assert order.order_id == "abc123-uuid"
    assert order.client_order_id == "abcdef0123456789"
    assert order.symbol == "AAPL"
    assert order.qty == Decimal("10")
    assert order.side == "buy"
    assert order.status == "accepted"


@pytest.mark.asyncio
async def test_submit_order_raises_alpaca_error_on_4xx() -> None:
    """A 4xx response from Alpaca MUST raise :class:`AlpacaError`
    carrying the venue body so the caller can preserve the rejection
    reason in BROKER_LIVE_REJECTED's payload."""
    with respx.mock(base_url=_PAPER_BASE_URL) as router:
        router.post("/v2/orders").mock(
            return_value=httpx.Response(
                422,
                json={"code": 40110000, "message": "insufficient buying power"},
            ),
        )
        async with AlpacaRestClient(api_key="k", api_secret="s") as client:
            with pytest.raises(AlpacaError) as excinfo:
                await client.submit_order(
                    symbol="AAPL",
                    qty=Decimal("10"),
                    side=Side.BUY,
                    client_order_id="abc",
                )
            assert excinfo.value.status_code == 422
            assert "insufficient buying power" in (excinfo.value.venue_body or "")


@pytest.mark.asyncio
async def test_submit_order_sends_client_order_id_in_body() -> None:
    """The client_order_id MUST be in the request body so Alpaca can
    deduplicate replays per ADR-0008 §NFR-LIVE-BROKER-4."""
    captured: dict[str, object] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "uuid",
                "client_order_id": str(captured.get("client_order_id", "")),
                "symbol": "AAPL",
                "qty": "1",
                "side": "buy",
                "status": "accepted",
            },
        )

    with respx.mock(base_url=_PAPER_BASE_URL) as router:
        router.post("/v2/orders").mock(side_effect=_capture)
        async with AlpacaRestClient(api_key="k", api_secret="s") as client:
            await client.submit_order(
                symbol="AAPL",
                qty=Decimal("1"),
                side=Side.BUY,
                client_order_id="my-deterministic-id-48-chars-or-fewer-aaaa",
            )
    assert captured["client_order_id"] == "my-deterministic-id-48-chars-or-fewer-aaaa"
    assert captured["symbol"] == "AAPL"
    assert captured["side"] == "buy"


@pytest.mark.asyncio
async def test_submit_order_rejects_oversized_client_order_id() -> None:
    """ADR-0009 mandates the leading 48 chars of compute_idempotency_key's
    64-char digest. The client MUST validate this at the API boundary —
    not silently let Alpaca reject it."""
    async with AlpacaRestClient(api_key="k", api_secret="s") as client:
        with pytest.raises(ValueError, match="48"):
            await client.submit_order(
                symbol="AAPL",
                qty=Decimal("1"),
                side=Side.BUY,
                client_order_id="a" * 49,  # one over
            )


@pytest.mark.asyncio
async def test_cancel_all_orders_does_not_raise_on_2xx() -> None:
    """DELETE /v2/orders is the kill-switch primitive. 2xx (any) MUST
    succeed without raising."""
    with respx.mock(base_url=_PAPER_BASE_URL) as router:
        router.delete("/v2/orders").mock(
            return_value=httpx.Response(207, json=[]),
        )
        async with AlpacaRestClient(api_key="k", api_secret="s") as client:
            await client.cancel_all_orders()


# NB: from_env tests are NOT xfailed in step 1 — the from_env classmethod
# is part of the step 1 surface (env reading is shippable now; only
# submit_order / cancel_all_orders await step 2 wire-up). Treating them
# as regression guards under xfail(strict=True) would xpass-fail.


@pytest.mark.asyncio
async def test_from_env_reads_live_broker_prefixed_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from_env MUST read LIVE_BROKER_API_KEY / LIVE_BROKER_API_SECRET
    per ADR-0008 §NFR-LIVE-BROKER-2. Unset key MUST raise."""
    monkeypatch.setenv("LIVE_BROKER_API_KEY", "key-123")
    monkeypatch.setenv("LIVE_BROKER_API_SECRET", "secret-456")
    monkeypatch.delenv("LIVE_BROKER_BASE_URL", raising=False)
    client = AlpacaRestClient.from_env()
    assert client._api_key == "key-123"
    assert client._api_secret == "secret-456"
    assert client._base_url == _PAPER_BASE_URL


def test_from_env_raises_when_api_key_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing LIVE_BROKER_API_KEY MUST surface as
    :class:`AlpacaError` — no silent default-to-empty."""
    monkeypatch.delenv("LIVE_BROKER_API_KEY", raising=False)
    monkeypatch.delenv("LIVE_BROKER_API_SECRET", raising=False)
    with pytest.raises(AlpacaError, match="LIVE_BROKER_API_KEY"):
        AlpacaRestClient.from_env()
