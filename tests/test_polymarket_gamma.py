"""Tests for the Polymarket Gamma client.

All HTTP calls mocked via respx. Live tests live in tests/live/.
"""

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import respx

from caqrs.data.polymarket import (
    GammaMarket,
    PolymarketError,
    PolymarketGammaClient,
)

_BASE = "https://gamma-api.polymarket.com"


# === Fixtures ===


def _market_payload(
    *,
    market_id: str = "12345",
    slug: str = "will-fed-cut-rates-2026",
    question: str = "Will the Fed cut rates in 2026?",
    outcomes: str | None = '["Yes", "No"]',
    clob_token_ids: str | None = '["100", "200"]',
    outcome_prices: str | None = '["0.62", "0.38"]',
    end_date: str | None = "2026-12-31T23:59:59Z",
    active: bool = True,
    closed: bool = False,
    volume: str | None = "12345.67",
    liquidity: str | None = "2500.00",
    last_trade_price: float | None = 0.62,
) -> dict[str, object]:
    body: dict[str, object] = {
        "id": market_id,
        "slug": slug,
        "question": question,
        "active": active,
        "closed": closed,
    }
    if outcomes is not None:
        body["outcomes"] = outcomes
    if clob_token_ids is not None:
        body["clobTokenIds"] = clob_token_ids
    if outcome_prices is not None:
        body["outcomePrices"] = outcome_prices
    if end_date is not None:
        body["endDate"] = end_date
    if volume is not None:
        body["volume"] = volume
    if liquidity is not None:
        body["liquidity"] = liquidity
    if last_trade_price is not None:
        body["lastTradePrice"] = last_trade_price
    return body


# === Identifier routing (numeric id vs slug) ===
#
# Polymarket's GET /markets/{id} only accepts numeric ids and returns
# 422 'id is invalid' when given a slug — the public API contradicts
# the surface-level docs that suggest the path accepts either. The
# client must dispatch: numeric → path-based, slug → GET /markets?slug=.


@pytest.mark.asyncio
@respx.mock
async def test_get_market_with_numeric_id_uses_path_endpoint() -> None:
    by_id_route = respx.get(f"{_BASE}/markets/12345").mock(
        return_value=httpx.Response(200, json=_market_payload(market_id="12345")),
    )
    list_route = respx.get(f"{_BASE}/markets").mock(
        return_value=httpx.Response(200, json=[]),
    )
    async with PolymarketGammaClient() as gamma:
        market = await gamma.get_market("12345")
    assert market.id == "12345"
    assert by_id_route.call_count == 1
    assert list_route.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_get_market_with_slug_uses_list_endpoint_with_slug_filter() -> None:
    slug = "will-fed-cut-rates-2026"
    list_route = respx.get(f"{_BASE}/markets").mock(
        return_value=httpx.Response(200, json=[_market_payload(slug=slug)]),
    )
    by_id_route = respx.get(f"{_BASE}/markets/{slug}").mock(
        return_value=httpx.Response(422, json={"error": "id is invalid"}),
    )
    async with PolymarketGammaClient() as gamma:
        market = await gamma.get_market(slug)
    assert market.slug == slug
    assert list_route.call_count == 1
    assert list_route.calls.last.request.url.params["slug"] == slug
    # Must not have hit the path-based endpoint with a slug
    assert by_id_route.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_get_market_by_slug_raises_when_no_match() -> None:
    respx.get(f"{_BASE}/markets").mock(
        return_value=httpx.Response(200, json=[]),
    )
    async with PolymarketGammaClient() as gamma:
        with pytest.raises(PolymarketError, match="not found"):
            await gamma.get_market("nonexistent-slug")


# === GET /markets/{id} (parsing) ===


@pytest.mark.asyncio
@respx.mock
async def test_get_market_decodes_string_arrays() -> None:
    respx.get(f"{_BASE}/markets/12345").mock(
        return_value=httpx.Response(200, json=_market_payload()),
    )
    async with PolymarketGammaClient() as gamma:
        market = await gamma.get_market("12345")

    assert market.id == "12345"
    assert market.slug == "will-fed-cut-rates-2026"
    assert market.question == "Will the Fed cut rates in 2026?"
    assert market.outcomes == ("Yes", "No")
    assert market.clob_token_ids == ("100", "200")
    assert market.outcome_prices == (Decimal("0.62"), Decimal("0.38"))
    assert market.end_date == datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)
    assert market.active is True
    assert market.closed is False
    assert market.volume == Decimal("12345.67")
    assert market.liquidity == Decimal("2500.00")
    assert market.last_trade_price == Decimal("0.62")


@pytest.mark.asyncio
@respx.mock
async def test_binary_market_convenience_properties() -> None:
    respx.get(f"{_BASE}/markets/12345").mock(
        return_value=httpx.Response(200, json=_market_payload()),
    )
    async with PolymarketGammaClient() as gamma:
        market = await gamma.get_market("12345")

    assert market.is_binary is True
    assert market.yes_token_id == "100"
    assert market.no_token_id == "200"
    assert market.yes_implied_prob == Decimal("0.62")
    assert market.no_implied_prob == Decimal("0.38")


@pytest.mark.asyncio
@respx.mock
async def test_multi_outcome_market_skips_binary_helpers() -> None:
    payload = _market_payload(
        outcomes='["Trump", "Biden", "Harris", "Other"]',
        clob_token_ids='["1", "2", "3", "4"]',
        outcome_prices='["0.45", "0.10", "0.30", "0.15"]',
    )
    respx.get(f"{_BASE}/markets/12345").mock(
        return_value=httpx.Response(200, json=payload),
    )
    async with PolymarketGammaClient() as gamma:
        market = await gamma.get_market("12345")

    assert market.is_binary is False
    assert market.yes_token_id is None
    assert market.yes_implied_prob is None
    # But per-outcome lookup still works
    assert market.token_id_for_outcome("Trump") is None  # is_binary guard
    # Direct array indexing still available
    assert market.outcomes == ("Trump", "Biden", "Harris", "Other")
    assert market.outcome_prices == (
        Decimal("0.45"),
        Decimal("0.10"),
        Decimal("0.30"),
        Decimal("0.15"),
    )


@pytest.mark.asyncio
@respx.mock
async def test_yes_no_lookup_is_case_insensitive() -> None:
    payload = _market_payload(outcomes='["yes", "no"]')
    respx.get(f"{_BASE}/markets/12345").mock(
        return_value=httpx.Response(200, json=payload),
    )
    async with PolymarketGammaClient() as gamma:
        market = await gamma.get_market("12345")
    assert market.yes_token_id == "100"


@pytest.mark.asyncio
@respx.mock
async def test_get_market_handles_missing_optional_fields() -> None:
    """Markets older than the schema-version-1 era can be missing
    most metadata; the client should not raise on absent optionals."""
    payload = {
        "id": "12345",
        "active": True,
        "closed": False,
    }
    respx.get(f"{_BASE}/markets/12345").mock(
        return_value=httpx.Response(200, json=payload),
    )
    async with PolymarketGammaClient() as gamma:
        market = await gamma.get_market("12345")
    assert market.id == "12345"
    assert market.outcomes == ()
    assert market.outcome_prices == ()
    assert market.last_trade_price is None
    assert market.end_date is None


@pytest.mark.asyncio
@respx.mock
async def test_get_market_accepts_already_decoded_arrays() -> None:
    """If Polymarket ever switches to native arrays the client should
    keep working."""
    payload = _market_payload(
        outcomes=None,
        clob_token_ids=None,
        outcome_prices=None,
    )
    payload["outcomes"] = ["Yes", "No"]
    payload["clobTokenIds"] = ["100", "200"]
    payload["outcomePrices"] = ["0.62", "0.38"]
    respx.get(f"{_BASE}/markets/12345").mock(
        return_value=httpx.Response(200, json=payload),
    )
    async with PolymarketGammaClient() as gamma:
        market = await gamma.get_market("12345")
    assert market.outcomes == ("Yes", "No")
    assert market.clob_token_ids == ("100", "200")


@pytest.mark.asyncio
@respx.mock
async def test_get_market_invalid_outcomes_string_raises() -> None:
    payload = _market_payload(outcomes="not-json{}")
    respx.get(f"{_BASE}/markets/12345").mock(
        return_value=httpx.Response(200, json=payload),
    )
    async with PolymarketGammaClient() as gamma:
        with pytest.raises(PolymarketError):
            await gamma.get_market("12345")


@pytest.mark.asyncio
@respx.mock
async def test_end_date_z_suffix_parses() -> None:
    payload = _market_payload(end_date="2026-01-15T12:00:00Z")
    respx.get(f"{_BASE}/markets/12345").mock(
        return_value=httpx.Response(200, json=payload),
    )
    async with PolymarketGammaClient() as gamma:
        market = await gamma.get_market("12345")
    assert market.end_date == datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


# === GET /markets (list) ===


@pytest.mark.asyncio
@respx.mock
async def test_list_markets_parses_array_response() -> None:
    respx.get(f"{_BASE}/markets").mock(
        return_value=httpx.Response(
            200,
            json=[
                _market_payload(market_id="1", slug="m1"),
                _market_payload(market_id="2", slug="m2"),
            ],
        ),
    )
    async with PolymarketGammaClient() as gamma:
        markets = await gamma.list_markets()
    assert len(markets) == 2
    assert all(isinstance(m, GammaMarket) for m in markets)
    assert markets[0].slug == "m1"
    assert markets[1].slug == "m2"


@pytest.mark.asyncio
@respx.mock
async def test_list_markets_parses_data_wrapped_response() -> None:
    """Tolerate both bare-array and {"data": [...]} response shapes."""
    respx.get(f"{_BASE}/markets").mock(
        return_value=httpx.Response(
            200,
            json={"data": [_market_payload(market_id="1", slug="m1")]},
        ),
    )
    async with PolymarketGammaClient() as gamma:
        markets = await gamma.list_markets()
    assert len(markets) == 1
    assert markets[0].slug == "m1"


@pytest.mark.asyncio
@respx.mock
async def test_list_markets_passes_filters_as_query_params() -> None:
    route = respx.get(f"{_BASE}/markets").mock(
        return_value=httpx.Response(200, json=[]),
    )
    async with PolymarketGammaClient() as gamma:
        await gamma.list_markets(
            limit=50,
            offset=100,
            slugs=("a", "b"),
            condition_ids=("0xCID",),
            clob_token_ids=("100",),
            active=True,
            closed=False,
            liquidity_num_min=1000.0,
            volume_num_min=500.0,
            tag_id=42,
        )
    params = route.calls.last.request.url.params
    assert params["limit"] == "50"
    assert params["offset"] == "100"
    # Repeated keys for list filters
    assert params.get_list("slug") == ["a", "b"]
    assert params.get_list("condition_ids") == ["0xCID"]
    assert params.get_list("clob_token_ids") == ["100"]
    # Booleans serialise to lowercase
    assert params["active"] == "true"
    assert params["closed"] == "false"
    assert params["liquidity_num_min"] == "1000.0"
    assert params["volume_num_min"] == "500.0"
    assert params["tag_id"] == "42"


@pytest.mark.asyncio
@respx.mock
async def test_list_markets_sends_iso_dates() -> None:
    route = respx.get(f"{_BASE}/markets").mock(
        return_value=httpx.Response(200, json=[]),
    )
    async with PolymarketGammaClient() as gamma:
        await gamma.list_markets(
            end_date_min=datetime(2026, 1, 1, tzinfo=UTC),
            end_date_max=datetime(2026, 12, 31, tzinfo=UTC),
        )
    params = route.calls.last.request.url.params
    assert params["end_date_min"] == "2026-01-01T00:00:00+00:00"
    assert params["end_date_max"] == "2026-12-31T00:00:00+00:00"


@pytest.mark.asyncio
@respx.mock
async def test_list_markets_omits_none_filters() -> None:
    """Verifies we don't accidentally send 'limit=None' on the wire."""
    route = respx.get(f"{_BASE}/markets").mock(
        return_value=httpx.Response(200, json=[]),
    )
    async with PolymarketGammaClient() as gamma:
        await gamma.list_markets()
    assert len(route.calls.last.request.url.params) == 0


# === Errors ===


@pytest.mark.asyncio
@respx.mock
async def test_404_raises_with_status_code() -> None:
    """Upstream HTTP 404 from the path-based /markets/{id} endpoint
    surfaces with status_code preserved."""
    respx.get(f"{_BASE}/markets/99999").mock(
        return_value=httpx.Response(404, text="not found"),
    )
    async with PolymarketGammaClient() as gamma:
        with pytest.raises(PolymarketError) as exc_info:
            await gamma.get_market("99999")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
@respx.mock
async def test_object_body_without_data_raises() -> None:
    respx.get(f"{_BASE}/markets").mock(
        return_value=httpx.Response(200, json={"unexpected": "shape"}),
    )
    async with PolymarketGammaClient() as gamma:
        with pytest.raises(PolymarketError):
            await gamma.list_markets()
