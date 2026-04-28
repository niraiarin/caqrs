"""EdinetDbClient cache integration.

Verifies that when an :class:`EdinetDbCache` is wired in:

- The first call hits HTTP and writes to cache.
- Subsequent calls within TTL hit cache and avoid HTTP entirely
  (critical for the 100 req/day free-plan budget).
- Each endpoint family (/companies, /financials, /rankings) caches
  independently.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from caqrs.data.edinetdb.cache import EdinetDbCache
from caqrs.data.edinetdb.client import EdinetDbClient
from caqrs.data.edinetdb.schemas import (
    EdinetDbCompaniesList,
    EdinetDbCompany,
    EdinetDbMeta,
    EdinetDbPagination,
)

_BASE = "https://edinetdb.jp/v1"


def _company_payload(edinet_code: str = "E03006") -> dict[str, object]:
    return {
        "data": [
            {
                "accounting_standard": "JP",
                "credit_rating": "S",
                "credit_score": 93,
                "edinet_code": edinet_code,
                "industry": "卸売業",
                "name": "Test Co",
                "name_en": "Test Co",
                "name_ja": "Test Co",
                "sec_code": "30760",
            },
        ],
        "meta": {"pagination": {"page": 1, "per_page": 1, "total": 1}},
    }


def _financials_payload(*, fiscal_year: int = 2024) -> dict[str, object]:
    return {
        "data": [
            {
                "accounting_standard": "JP",
                "fiscal_year": fiscal_year,
                "revenue": 1000.0,
                "net_income": 100.0,
                "ordinary_income": None,
                "comprehensive_income": None,
                "total_assets": None,
                "total_liabilities": None,
                "net_assets": None,
                "cash": None,
                "cf_operating": None,
                "cf_investing": None,
                "cf_financing": None,
                "eps": None,
                "bps": None,
                "adjusted_eps": None,
                "adjusted_bps": None,
                "adjusted_dividend_per_share": None,
                "dividend_per_share": None,
                "equity_ratio_official": None,
                "shares_issued": None,
                "split_adjustment_factor": None,
                "num_employees": None,
                "temp_employees": None,
                "is_restated_eps": False,
                "is_restated_bps": False,
                "is_restated_diluted_eps": False,
            },
        ],
    }


def _ranking_payload() -> dict[str, object]:
    return {
        "data": [
            {
                "edinet_code": "E40919",
                "fiscal_year": 2025,
                "industry": "情報・通信業",
                "name": "ウリドキ株式会社",
                "name_en": None,
                "name_ja": "ウリドキ株式会社",
                "rank": 1,
                "sec_code": "418A0",
                "unit": "%",
                "value": 84.39,
            },
        ],
    }


# === list_companies ===


@pytest.mark.asyncio
@respx.mock
async def test_list_companies_writes_to_cache_on_miss(tmp_path: Path) -> None:
    cache = EdinetDbCache(db_path=tmp_path / "edinetdb.db")
    route = respx.get(f"{_BASE}/companies").mock(
        return_value=httpx.Response(200, json=_company_payload()),
    )

    async with EdinetDbClient(api_key="k", throttle_seconds=0, cache=cache) as client:
        first = await client.list_companies(page=1, per_page=1)
        # Second call hits cache; no second HTTP request.
        second = await client.list_companies(page=1, per_page=1)

    assert route.call_count == 1
    assert first == second


@pytest.mark.asyncio
@respx.mock
async def test_list_companies_pagination_keys_dont_collide(tmp_path: Path) -> None:
    """Different page args result in distinct cache keys; only the
    first request per page actually hits HTTP."""
    cache = EdinetDbCache(db_path=tmp_path / "edinetdb.db")
    route = respx.get(f"{_BASE}/companies").mock(
        return_value=httpx.Response(200, json=_company_payload()),
    )

    async with EdinetDbClient(api_key="k", throttle_seconds=0, cache=cache) as client:
        await client.list_companies(page=1, per_page=10)
        await client.list_companies(page=2, per_page=10)
        await client.list_companies(page=1, per_page=10)  # cached
        await client.list_companies(page=2, per_page=10)  # cached

    # 2 distinct (page, per_page) pairs -> 2 HTTP calls, the rest cached.
    assert route.call_count == 2


# === company_financials ===


@pytest.mark.asyncio
@respx.mock
async def test_company_financials_writes_to_cache(tmp_path: Path) -> None:
    cache = EdinetDbCache(db_path=tmp_path / "edinetdb.db")
    route = respx.get(f"{_BASE}/companies/E02144/financials").mock(
        return_value=httpx.Response(200, json=_financials_payload()),
    )

    async with EdinetDbClient(api_key="k", throttle_seconds=0, cache=cache) as client:
        first = await client.company_financials(edinet_code="E02144")
        second = await client.company_financials(edinet_code="E02144")

    assert route.call_count == 1
    assert first == second


@pytest.mark.asyncio
@respx.mock
async def test_different_edinet_codes_cache_independently(tmp_path: Path) -> None:
    cache = EdinetDbCache(db_path=tmp_path / "edinetdb.db")
    respx.get(f"{_BASE}/companies/E02144/financials").mock(
        return_value=httpx.Response(200, json=_financials_payload(fiscal_year=2024)),
    )
    respx.get(f"{_BASE}/companies/E03006/financials").mock(
        return_value=httpx.Response(200, json=_financials_payload(fiscal_year=2023)),
    )

    async with EdinetDbClient(api_key="k", throttle_seconds=0, cache=cache) as client:
        a = await client.company_financials(edinet_code="E02144")
        b = await client.company_financials(edinet_code="E03006")
        # Repeats hit cache.
        a_again = await client.company_financials(edinet_code="E02144")
        b_again = await client.company_financials(edinet_code="E03006")

    assert a == a_again
    assert b == b_again
    assert a != b


# === ranking_roe ===


@pytest.mark.asyncio
@respx.mock
async def test_ranking_roe_writes_to_cache(tmp_path: Path) -> None:
    cache = EdinetDbCache(db_path=tmp_path / "edinetdb.db")
    route = respx.get(f"{_BASE}/rankings/roe").mock(
        return_value=httpx.Response(200, json=_ranking_payload()),
    )

    async with EdinetDbClient(api_key="k", throttle_seconds=0, cache=cache) as client:
        first = await client.ranking_roe(limit=1)
        second = await client.ranking_roe(limit=1)

    assert route.call_count == 1
    assert first == second


@pytest.mark.asyncio
@respx.mock
async def test_different_limits_cache_independently(tmp_path: Path) -> None:
    cache = EdinetDbCache(db_path=tmp_path / "edinetdb.db")
    route = respx.get(f"{_BASE}/rankings/roe").mock(
        return_value=httpx.Response(200, json=_ranking_payload()),
    )

    async with EdinetDbClient(api_key="k", throttle_seconds=0, cache=cache) as client:
        await client.ranking_roe(limit=1)
        await client.ranking_roe(limit=10)
        # Repeats hit cache.
        await client.ranking_roe(limit=1)
        await client.ranking_roe(limit=10)

    assert route.call_count == 2


# === Pre-populated cache means HTTP is never invoked ===


@pytest.mark.asyncio
async def test_prepopulated_cache_avoids_http_entirely(tmp_path: Path) -> None:
    """The whole point of the cache: a prefetch script populates the
    file once per day, then cycle runs serve every read from cache
    and never touch the 100 req/day quota."""
    cache = EdinetDbCache(db_path=tmp_path / "edinetdb.db")

    pre = EdinetDbCompaniesList(
        data=(
            EdinetDbCompany(
                edinet_code="E03006",
                sec_code="30760",
                name="X",
                name_en="X",
                name_ja="X",
                industry="卸売業",
                accounting_standard="JP",
                credit_rating="S",
                credit_score=93,
            ),
        ),
        meta=EdinetDbMeta(pagination=EdinetDbPagination(page=1, per_page=1, total=1)),
    )
    cache.set_companies(page=1, per_page=1, listing=pre, ttl_seconds=86_400)

    # If the client tried HTTP, this MagicMock would explode — proving
    # cache-only behaviour.
    no_http = MagicMock()
    no_http.get = MagicMock(side_effect=AssertionError("HTTP must not be called"))

    client = EdinetDbClient(api_key="k", throttle_seconds=0, cache=cache)
    client._client = no_http
    result = await client.list_companies(page=1, per_page=1)
    assert result == pre
