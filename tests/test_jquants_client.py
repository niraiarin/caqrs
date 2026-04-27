"""Tests for JQuantsClient — V2 API async wrapper."""

from datetime import date

import httpx
import pytest
import respx

from caqrs.data.jquants import JQuantsClient, JQuantsError

_BASE = "https://api.jquants.com/v2"
_KEY = "jq-test-key"


# === Auth ===


@pytest.mark.asyncio
@respx.mock
async def test_request_carries_api_key_header() -> None:
    route = respx.get(f"{_BASE}/equities/master").mock(
        return_value=httpx.Response(200, json={"data": []}),
    )
    async with JQuantsClient(api_key=_KEY) as client:
        await client.list_master()
    assert route.calls.last.request.headers["x-api-key"] == _KEY


@pytest.mark.asyncio
@respx.mock
async def test_401_raises_with_status_code() -> None:
    respx.get(f"{_BASE}/equities/master").mock(
        return_value=httpx.Response(401, text="invalid api key"),
    )
    async with JQuantsClient(api_key=_KEY) as client:
        with pytest.raises(JQuantsError) as exc_info:
            await client.list_master()
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
@respx.mock
async def test_429_rate_limit_raises_with_status_code() -> None:
    respx.get(f"{_BASE}/equities/master").mock(
        return_value=httpx.Response(429, text="too many requests"),
    )
    async with JQuantsClient(api_key=_KEY) as client:
        with pytest.raises(JQuantsError) as exc_info:
            await client.list_master()
    assert exc_info.value.status_code == 429


# === list_master ===


@pytest.mark.asyncio
@respx.mock
async def test_list_master_parses_typed_records() -> None:
    respx.get(f"{_BASE}/equities/master").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"Date": "2025-04-25", "Code": "13010", "CoName": "極洋"},
                    {"Date": "2025-04-25", "Code": "72030", "CoName": "トヨタ自動車"},
                ],
            },
        ),
    )
    async with JQuantsClient(api_key=_KEY) as client:
        rows = await client.list_master()
    assert len(rows) == 2
    assert rows[0].code == "13010"
    assert rows[1].company_name == "トヨタ自動車"


@pytest.mark.asyncio
@respx.mock
async def test_list_master_passes_code_and_date_query_params() -> None:
    route = respx.get(f"{_BASE}/equities/master").mock(
        return_value=httpx.Response(200, json={"data": []}),
    )
    async with JQuantsClient(api_key=_KEY) as client:
        await client.list_master(code="13010", as_of=date(2025, 4, 25))
    params = route.calls.last.request.url.params
    assert params["code"] == "13010"
    # J-Quants accepts YYYYMMDD; the client encodes date that way.
    assert params["date"] == "20250425"


# === Pagination ===


@pytest.mark.asyncio
@respx.mock
async def test_pagination_follows_pagination_key() -> None:
    """The client transparently follows pagination_key until the API
    omits it from the response."""
    page1 = {"data": [{"Date": "2025-04-25", "Code": "1", "CoName": "A"}], "pagination_key": "p1"}
    page2 = {"data": [{"Date": "2025-04-25", "Code": "2", "CoName": "B"}], "pagination_key": "p2"}
    page3 = {"data": [{"Date": "2025-04-25", "Code": "3", "CoName": "C"}]}  # no key → end

    def _handler(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("pagination_key")
        if token is None:
            return httpx.Response(200, json=page1)
        if token == "p1":
            return httpx.Response(200, json=page2)
        if token == "p2":
            return httpx.Response(200, json=page3)
        return httpx.Response(500)

    respx.get(f"{_BASE}/equities/master").mock(side_effect=_handler)

    async with JQuantsClient(api_key=_KEY) as client:
        rows = await client.list_master()
    assert [r.code for r in rows] == ["1", "2", "3"]


@pytest.mark.asyncio
@respx.mock
async def test_pagination_preserves_other_query_params_across_pages() -> None:
    """A user-supplied query parameter (e.g. code) must reach every page."""
    captured_codes: list[str | None] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured_codes.append(request.url.params.get("code"))
        token = request.url.params.get("pagination_key")
        if token is None:
            return httpx.Response(
                200,
                json={
                    "data": [{"Date": "2025-04-25", "Code": "13010", "CoName": "A"}],
                    "pagination_key": "p1",
                },
            )
        return httpx.Response(
            200,
            json={"data": [{"Date": "2025-04-25", "Code": "13010", "CoName": "A"}]},
        )

    respx.get(f"{_BASE}/equities/master").mock(side_effect=_handler)

    async with JQuantsClient(api_key=_KEY) as client:
        await client.list_master(code="13010")
    assert captured_codes == ["13010", "13010"]


# === daily_bars ===


@pytest.mark.asyncio
@respx.mock
async def test_daily_bars_parses_typed_records() -> None:
    respx.get(f"{_BASE}/equities/bars/daily").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "Date": "2025-04-25",
                        "Code": "13010",
                        "O": "100.0",
                        "H": "110.0",
                        "L": "90.0",
                        "C": "105.0",
                        "Vo": 1000,
                    },
                ],
            },
        ),
    )
    async with JQuantsClient(api_key=_KEY) as client:
        bars = await client.daily_bars(code="13010", as_of=date(2025, 4, 25))
    assert len(bars) == 1
    assert bars[0].code == "13010"


@pytest.mark.asyncio
@respx.mock
async def test_daily_bars_date_range_uses_from_to_params() -> None:
    route = respx.get(f"{_BASE}/equities/bars/daily").mock(
        return_value=httpx.Response(200, json={"data": []}),
    )
    async with JQuantsClient(api_key=_KEY) as client:
        await client.daily_bars(
            code="13010",
            from_date=date(2025, 1, 1),
            to_date=date(2025, 4, 30),
        )
    params = route.calls.last.request.url.params
    assert params["code"] == "13010"
    assert params["from"] == "20250101"
    assert params["to"] == "20250430"
    assert "date" not in params


@pytest.mark.asyncio
@respx.mock
async def test_daily_bars_as_of_takes_precedence_over_range() -> None:
    """When both as_of and from/to are supplied, as_of wins (matches the
    upstream API which rejects the combination)."""
    route = respx.get(f"{_BASE}/equities/bars/daily").mock(
        return_value=httpx.Response(200, json={"data": []}),
    )
    async with JQuantsClient(api_key=_KEY) as client:
        await client.daily_bars(
            code="13010",
            as_of=date(2025, 4, 25),
            from_date=date(2025, 1, 1),
            to_date=date(2025, 4, 30),
        )
    params = route.calls.last.request.url.params
    assert params["date"] == "20250425"
    assert "from" not in params
    assert "to" not in params


# === Misc ===


@pytest.mark.asyncio
async def test_constructor_rejects_empty_api_key() -> None:
    with pytest.raises(ValueError, match="api_key"):
        JQuantsClient(api_key="")


@pytest.mark.asyncio
@respx.mock
async def test_network_error_wrapped_as_jquants_error() -> None:
    respx.get(f"{_BASE}/equities/master").mock(
        side_effect=httpx.ConnectError("boom"),
    )
    async with JQuantsClient(api_key=_KEY) as client:
        with pytest.raises(JQuantsError):
            await client.list_master()
