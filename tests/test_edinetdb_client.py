"""EDINET DB (edinetdb.jp) async client behaviour.

The client wraps the v1 endpoints documented at edinetdb.jp:

- ``GET /v1/companies?per_page=N&page=N`` — paginated company list
- ``GET /v1/companies/{edinet_code}/financials`` — fiscal-year history
- ``GET /v1/rankings/roe?limit=N`` — ROE leaderboard

Auth is an ``X-API-Key`` HTTP header (NOT a query parameter, unlike
the official EDINET API).
"""

from __future__ import annotations

import os
from unittest.mock import patch

import httpx
import pytest
import respx

from caqrs.data.edinetdb.client import EdinetDbClient, EdinetDbError

_BASE = "https://edinetdb.jp/v1"


# === Fixtures ===


def _company_record(*, edinet_code: str = "E03006") -> dict[str, object]:
    return {
        "accounting_standard": "JP",
        "credit_rating": "S",
        "credit_score": 93,
        "edinet_code": edinet_code,
        "industry": "卸売業",
        "name": "あいホールディングス株式会社",
        "name_en": "Ai Holdings Corporation",
        "name_ja": "あいホールディングス株式会社",
        "sec_code": "30760",
    }


def _companies_payload(*, count: int = 1) -> dict[str, object]:
    return {
        "data": [_company_record(edinet_code=f"E0300{i}") for i in range(count)],
        "meta": {
            "pagination": {
                "page": 1,
                "per_page": count,
                "total": count,
            },
        },
    }


def _financial_record(*, fiscal_year: int = 2024) -> dict[str, object]:
    return {
        "accounting_standard": "JP",
        "fiscal_year": fiscal_year,
        "revenue": 647652000000.0,
        "net_income": -43204000000.0,
        "ordinary_income": -60863000000.0,
        "comprehensive_income": -51045000000.0,
        "total_assets": 1368401000000.0,
        "total_liabilities": 177376000000.0,
        "net_assets": 1191025000000.0,
        "cash": 407186000000.0,
        "cf_operating": -94955000000.0,
        "cf_investing": -164392000000.0,
        "cf_financing": -39823000000.0,
        "eps": -337.86,
        "bps": 9313.15,
        "adjusted_eps": -30.496438882183114,
        "adjusted_bps": 931.3149999999998,
        "adjusted_dividend_per_share": 9.999999999999998,
        "dividend_per_share": 100.0,
        "equity_ratio_official": 0.8703,
        "shares_issued": 141669000.0,
        "split_adjustment_factor": 10.000000000000002,
        "num_employees": 4928,
        "temp_employees": 197,
        "is_restated_bps": False,
        "is_restated_diluted_eps": False,
        "is_restated_eps": False,
    }


def _ranking_record(*, rank: int = 1) -> dict[str, object]:
    return {
        "edinet_code": "E40919",
        "fiscal_year": 2025,
        "industry": "情報・通信業",
        "name": "ウリドキ株式会社",
        "name_en": None,
        "name_ja": "ウリドキ株式会社",
        "rank": rank,
        "sec_code": "418A0",
        "unit": "%",
        "value": 84.3889,
    }


# === list_companies ===


@pytest.mark.asyncio
@respx.mock
async def test_list_companies_returns_typed_records() -> None:
    route = respx.get(f"{_BASE}/companies").mock(
        return_value=httpx.Response(200, json=_companies_payload(count=3)),
    )

    async with EdinetDbClient(api_key="my-key") as client:
        listing = await client.list_companies(per_page=3)

    assert route.called
    # Auth via header, not query param.
    assert route.calls.last.request.headers.get("X-API-Key") == "my-key"
    assert route.calls.last.request.url.params["per_page"] == "3"
    assert len(listing.data) == 3
    assert listing.data[0].edinet_code == "E03000"
    assert listing.meta.pagination.per_page == 3


@pytest.mark.asyncio
@respx.mock
async def test_list_companies_passes_pagination_params() -> None:
    route = respx.get(f"{_BASE}/companies").mock(
        return_value=httpx.Response(200, json=_companies_payload()),
    )
    async with EdinetDbClient(api_key="my-key") as client:
        await client.list_companies(page=5, per_page=50)

    params = route.calls.last.request.url.params
    assert params["page"] == "5"
    assert params["per_page"] == "50"


# === company_financials ===


@pytest.mark.asyncio
@respx.mock
async def test_company_financials_returns_typed_records() -> None:
    payload = {
        "data": [_financial_record(fiscal_year=2022), _financial_record(fiscal_year=2023)],
    }
    respx.get(f"{_BASE}/companies/E02367/financials").mock(
        return_value=httpx.Response(200, json=payload),
    )
    async with EdinetDbClient(api_key="my-key") as client:
        rows = await client.company_financials(edinet_code="E02367")

    assert len(rows) == 2
    assert rows[0].fiscal_year == 2022
    assert rows[1].fiscal_year == 2023
    # Sanity-check coercion of float -> Decimal.
    assert rows[0].equity_ratio_official is not None


# === ranking_roe ===


@pytest.mark.asyncio
@respx.mock
async def test_ranking_roe_returns_sorted_records() -> None:
    payload = {
        "data": [_ranking_record(rank=1), _ranking_record(rank=2), _ranking_record(rank=3)],
    }
    route = respx.get(f"{_BASE}/rankings/roe").mock(
        return_value=httpx.Response(200, json=payload),
    )
    async with EdinetDbClient(api_key="my-key") as client:
        rows = await client.ranking_roe(limit=3)

    assert route.calls.last.request.url.params["limit"] == "3"
    assert len(rows) == 3
    assert rows[0].rank == 1
    assert rows[2].rank == 3


# === Auth ===


@pytest.mark.asyncio
async def test_client_requires_api_key_at_first_call() -> None:
    async with EdinetDbClient(api_key=None) as client:
        with pytest.raises(EdinetDbError, match="api_key"):
            await client.list_companies()


def test_from_env_reads_edinetdb_api_key() -> None:
    with patch.dict(os.environ, {"EDINETDB_API_KEY": "env-supplied"}, clear=False):
        client = EdinetDbClient.from_env()
    assert client.api_key == "env-supplied"


def test_from_env_raises_when_missing() -> None:
    env = {k: v for k, v in os.environ.items() if k != "EDINETDB_API_KEY"}
    with (
        patch.dict(os.environ, env, clear=True),
        pytest.raises(EdinetDbError, match="EDINETDB_API_KEY"),
    ):
        EdinetDbClient.from_env()


# === Errors ===


@pytest.mark.asyncio
@respx.mock
async def test_401_raises_typed_error() -> None:
    """Live edinetdb.jp returns ``{statusCode, message}`` on 401 — same
    camelCase shape that bit us on the official EDINET API. Detect and
    raise as EdinetDbError."""
    respx.get(f"{_BASE}/companies").mock(
        return_value=httpx.Response(
            401,
            json={
                "statusCode": 401,
                "message": (
                    "Access denied due to invalid subscription key. "
                    "Make sure to provide a valid key for an active subscription."
                ),
            },
        ),
    )
    async with EdinetDbClient(api_key="bad") as client:
        with pytest.raises(EdinetDbError, match="401"):
            await client.list_companies()


@pytest.mark.asyncio
@respx.mock
async def test_404_raises_typed_error() -> None:
    """Unknown edinet_code → 404. The client surfaces the upstream
    message directly so callers can pattern-match."""
    respx.get(f"{_BASE}/companies/UNKNOWN/financials").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"}),
    )
    async with EdinetDbClient(api_key="my-key") as client:
        with pytest.raises(EdinetDbError, match="404"):
            await client.company_financials(edinet_code="UNKNOWN")


# === Throttle ===


@pytest.mark.asyncio
@respx.mock
async def test_throttle_zero_disables_pacing() -> None:
    respx.get(f"{_BASE}/companies").mock(
        return_value=httpx.Response(200, json=_companies_payload()),
    )
    async with EdinetDbClient(api_key="k", throttle_seconds=0) as client:
        for _ in range(5):
            await client.list_companies()
