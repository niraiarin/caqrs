"""Tests for the Polymarket CLOB client.

All HTTP calls are mocked via respx so nothing in this file talks to
the real network. Live smoke testing lives in ``tests/live/``.
"""

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import respx

from caqrs.data.polymarket import (
    PolymarketClobClient,
    PolymarketError,
    PriceHistoryInterval,
    Side,
)

_BASE = "https://clob.polymarket.com"
_TOKEN = "0xabc123"


# === /midpoint ===


@pytest.mark.asyncio
@respx.mock
async def test_get_midpoint_returns_decimal_from_string_response() -> None:
    respx.get(f"{_BASE}/midpoint").mock(
        return_value=httpx.Response(200, json={"mid_price": "0.45"}),
    )
    async with PolymarketClobClient() as clob:
        mid = await clob.get_midpoint(token_id=_TOKEN)
    assert mid == Decimal("0.45")


@pytest.mark.asyncio
@respx.mock
async def test_get_midpoint_accepts_float_response() -> None:
    """Some endpoints return numbers; the client coerces both."""
    respx.get(f"{_BASE}/midpoint").mock(
        return_value=httpx.Response(200, json={"mid_price": 0.5}),
    )
    async with PolymarketClobClient() as clob:
        mid = await clob.get_midpoint(token_id=_TOKEN)
    assert mid == Decimal("0.5")


@pytest.mark.asyncio
@respx.mock
async def test_get_midpoint_accepts_short_mid_key() -> None:
    """Live Polymarket returns the midpoint under the key ``mid``;
    the docs example shows ``mid_price``. Accept both."""
    respx.get(f"{_BASE}/midpoint").mock(
        return_value=httpx.Response(200, json={"mid": "0.535"}),
    )
    async with PolymarketClobClient() as clob:
        mid = await clob.get_midpoint(token_id=_TOKEN)
    assert mid == Decimal("0.535")


@pytest.mark.asyncio
@respx.mock
async def test_get_midpoint_passes_token_id_query_param() -> None:
    route = respx.get(f"{_BASE}/midpoint").mock(
        return_value=httpx.Response(200, json={"mid_price": "0.5"}),
    )
    async with PolymarketClobClient() as clob:
        await clob.get_midpoint(token_id=_TOKEN)
    assert route.calls.last.request.url.params["token_id"] == _TOKEN


# === /price ===


@pytest.mark.asyncio
@respx.mock
async def test_get_price_returns_decimal() -> None:
    respx.get(f"{_BASE}/price").mock(
        return_value=httpx.Response(200, json={"price": 0.42}),
    )
    async with PolymarketClobClient() as clob:
        price = await clob.get_price(token_id=_TOKEN, side=Side.BUY)
    assert price == Decimal("0.42")


@pytest.mark.asyncio
@respx.mock
async def test_get_price_passes_side_query_param() -> None:
    route = respx.get(f"{_BASE}/price").mock(
        return_value=httpx.Response(200, json={"price": 0.5}),
    )
    async with PolymarketClobClient() as clob:
        await clob.get_price(token_id=_TOKEN, side=Side.SELL)
    assert route.calls.last.request.url.params["side"] == "SELL"


# === /book ===


@pytest.mark.asyncio
@respx.mock
async def test_get_orderbook_parses_full_snapshot() -> None:
    respx.get(f"{_BASE}/book").mock(
        return_value=httpx.Response(
            200,
            json={
                "market": "0xmarket",
                "asset_id": _TOKEN,
                "timestamp": "1735689600",  # 2025-01-01 00:00:00 UTC
                "hash": "abc",
                "bids": [
                    {"price": "0.45", "size": "100"},
                    {"price": "0.44", "size": "200"},
                ],
                "asks": [
                    {"price": "0.46", "size": "150"},
                    {"price": "0.47", "size": "250"},
                ],
                "min_order_size": "1",
                "tick_size": "0.01",
                "neg_risk": False,
                "last_trade_price": "0.45",
            },
        ),
    )
    async with PolymarketClobClient() as clob:
        book = await clob.get_orderbook(token_id=_TOKEN)
    assert book.market == "0xmarket"
    assert book.asset_id == _TOKEN
    assert book.timestamp == datetime(2025, 1, 1, tzinfo=UTC)
    assert len(book.bids) == 2
    assert book.bids[0].price == Decimal("0.45")
    assert book.bids[0].size == Decimal("100")
    assert book.tick_size == Decimal("0.01")
    assert book.last_trade_price == Decimal("0.45")
    assert book.neg_risk is False


@pytest.mark.asyncio
@respx.mock
async def test_get_orderbook_parses_millisecond_timestamp() -> None:
    """Live Polymarket returns timestamps in milliseconds even though
    the docs example shows seconds. The parser must auto-detect."""
    respx.get(f"{_BASE}/book").mock(
        return_value=httpx.Response(
            200,
            json={
                "market": "0xmarket",
                "asset_id": _TOKEN,
                "timestamp": "1735689600000",  # ms = 2025-01-01 00:00:00 UTC
                "bids": [],
                "asks": [],
                "min_order_size": "1",
                "tick_size": "0.01",
                "neg_risk": False,
            },
        ),
    )
    async with PolymarketClobClient() as clob:
        book = await clob.get_orderbook(token_id=_TOKEN)
    assert book.timestamp == datetime(2025, 1, 1, tzinfo=UTC)


@pytest.mark.asyncio
@respx.mock
async def test_orderbook_derived_properties() -> None:
    respx.get(f"{_BASE}/book").mock(
        return_value=httpx.Response(
            200,
            json={
                "market": "0xmarket",
                "asset_id": _TOKEN,
                "timestamp": "1735689600",
                "bids": [{"price": "0.45", "size": "100"}],
                "asks": [{"price": "0.55", "size": "100"}],
                "min_order_size": "1",
                "tick_size": "0.01",
                "neg_risk": False,
            },
        ),
    )
    async with PolymarketClobClient() as clob:
        book = await clob.get_orderbook(token_id=_TOKEN)
    assert book.best_bid == Decimal("0.45")
    assert book.best_ask == Decimal("0.55")
    assert book.midpoint == Decimal("0.5")
    assert book.spread == Decimal("0.10")


@pytest.mark.asyncio
@respx.mock
async def test_orderbook_derived_properties_empty_book() -> None:
    respx.get(f"{_BASE}/book").mock(
        return_value=httpx.Response(
            200,
            json={
                "market": "0xmarket",
                "asset_id": _TOKEN,
                "timestamp": "1735689600",
                "bids": [],
                "asks": [],
                "min_order_size": "1",
                "tick_size": "0.01",
                "neg_risk": False,
            },
        ),
    )
    async with PolymarketClobClient() as clob:
        book = await clob.get_orderbook(token_id=_TOKEN)
    assert book.best_bid is None
    assert book.best_ask is None
    assert book.midpoint is None
    assert book.spread is None


# === /prices-history ===


@pytest.mark.asyncio
@respx.mock
async def test_get_price_history_parses_history_array() -> None:
    respx.get(f"{_BASE}/prices-history").mock(
        return_value=httpx.Response(
            200,
            json={
                "history": [
                    {"t": 1735689600, "p": 0.40},
                    {"t": 1735693200, "p": 0.42},
                    {"t": 1735696800, "p": 0.45},
                ],
            },
        ),
    )
    async with PolymarketClobClient() as clob:
        history = await clob.get_price_history(
            token_id=_TOKEN,
            interval=PriceHistoryInterval.ONE_HOUR,
        )
    assert history.asset_id == _TOKEN
    assert history.interval == "1h"
    assert len(history.points) == 3
    assert history.earliest is not None
    assert history.latest is not None
    assert history.earliest.price == Decimal("0.40")
    assert history.latest.price == Decimal("0.45")
    assert history.earliest.timestamp == datetime(2025, 1, 1, tzinfo=UTC)


@pytest.mark.asyncio
@respx.mock
async def test_price_history_uses_market_query_param_not_token_id() -> None:
    """Polymarket's prices-history endpoint takes 'market' even though the value is a token id."""
    route = respx.get(f"{_BASE}/prices-history").mock(
        return_value=httpx.Response(200, json={"history": []}),
    )
    async with PolymarketClobClient() as clob:
        await clob.get_price_history(token_id=_TOKEN, interval=PriceHistoryInterval.ONE_DAY)
    last = route.calls.last.request
    assert last.url.params["market"] == _TOKEN
    assert "token_id" not in last.url.params


@pytest.mark.asyncio
@respx.mock
async def test_price_history_passes_optional_params() -> None:
    route = respx.get(f"{_BASE}/prices-history").mock(
        return_value=httpx.Response(200, json={"history": []}),
    )
    async with PolymarketClobClient() as clob:
        await clob.get_price_history(
            token_id=_TOKEN,
            interval=PriceHistoryInterval.ONE_DAY,
            fidelity_minutes=15,
            start_ts=1735689600,
            end_ts=1735776000,
        )
    params = route.calls.last.request.url.params
    assert params["fidelity"] == "15"
    assert params["startTs"] == "1735689600"
    assert params["endTs"] == "1735776000"


# === Errors ===


@pytest.mark.asyncio
@respx.mock
async def test_non_200_status_raises_with_status_code() -> None:
    respx.get(f"{_BASE}/midpoint").mock(
        return_value=httpx.Response(404, text="not found"),
    )
    async with PolymarketClobClient() as clob:
        with pytest.raises(PolymarketError) as exc_info:
            await clob.get_midpoint(token_id=_TOKEN)
    assert exc_info.value.status_code == 404
    assert "404" in str(exc_info.value)


@pytest.mark.asyncio
@respx.mock
async def test_network_error_is_wrapped() -> None:
    respx.get(f"{_BASE}/midpoint").mock(side_effect=httpx.ConnectError("boom"))
    async with PolymarketClobClient() as clob:
        with pytest.raises(PolymarketError) as exc_info:
            await clob.get_midpoint(token_id=_TOKEN)
    assert "ConnectError" in str(exc_info.value)


@pytest.mark.asyncio
@respx.mock
async def test_invalid_decimal_in_response_raises() -> None:
    respx.get(f"{_BASE}/midpoint").mock(
        return_value=httpx.Response(200, json={"mid_price": "not-a-number"}),
    )
    async with PolymarketClobClient() as clob:
        with pytest.raises(PolymarketError):
            await clob.get_midpoint(token_id=_TOKEN)


@pytest.mark.asyncio
@respx.mock
async def test_missing_field_raises() -> None:
    respx.get(f"{_BASE}/midpoint").mock(
        return_value=httpx.Response(200, json={"some_other_field": "0.5"}),
    )
    async with PolymarketClobClient() as clob:
        with pytest.raises(PolymarketError):
            await clob.get_midpoint(token_id=_TOKEN)


# === External http client lifecycle ===


@pytest.mark.asyncio
@respx.mock
async def test_external_http_client_is_not_closed() -> None:
    """When the caller supplies an httpx.AsyncClient the client must not close it."""
    respx.get(f"{_BASE}/midpoint").mock(
        return_value=httpx.Response(200, json={"mid_price": "0.5"}),
    )
    async with httpx.AsyncClient() as http:
        clob = PolymarketClobClient(http_client=http)
        # Should be usable without entering CLOB's own context manager
        mid = await clob.get_midpoint(token_id=_TOKEN)
        assert mid == Decimal("0.5")
        # http is still alive — re-use should still succeed
        mid2 = await clob.get_midpoint(token_id=_TOKEN)
        assert mid2 == Decimal("0.5")
    # After leaving the outer context the http client is closed by its
    # owner; the test's expectation is that the CLOB client never tried
    # to close it itself.


@pytest.mark.asyncio
@respx.mock
async def test_used_without_context_manager_creates_one_shot_client() -> None:
    respx.get(f"{_BASE}/midpoint").mock(
        return_value=httpx.Response(200, json={"mid_price": "0.5"}),
    )
    clob = PolymarketClobClient()
    mid = await clob.get_midpoint(token_id=_TOKEN)
    assert mid == Decimal("0.5")
