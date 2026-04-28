"""EdinetDbClient + DailyQuotaTracker integration.

Verifies the wiring contract:

- Every successful HTTP fetch records one request in the tracker.
- Cache hits do NOT count (the cache shields the daily budget).
- When the daily cap is reached, the next attempted fetch raises
  :class:`EdinetDbQuotaExhaustedError` *before* any HTTP call.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from caqrs.data.edinetdb.cache import EdinetDbCache
from caqrs.data.edinetdb.client import EdinetDbClient, EdinetDbError
from caqrs.data.edinetdb.quota import (
    DailyQuotaTracker,
    EdinetDbQuotaExhaustedError,
)

_BASE = "https://edinetdb.jp/v1"


def _company_payload() -> dict[str, object]:
    return {
        "data": [
            {
                "accounting_standard": "JP",
                "credit_rating": "S",
                "credit_score": 93,
                "edinet_code": "E03006",
                "industry": "卸売業",
                "name": "Test Co",
                "name_en": "Test Co",
                "name_ja": "Test Co",
                "sec_code": "30760",
            },
        ],
        "meta": {"pagination": {"page": 1, "per_page": 1, "total": 1}},
    }


# === Successful fetch increments the tracker ===


@pytest.mark.asyncio
@respx.mock
async def test_successful_fetch_records_one_request(tmp_path: Path) -> None:
    tracker = DailyQuotaTracker(db_path=tmp_path / "q.db", daily_cap=100)
    respx.get(f"{_BASE}/companies").mock(
        return_value=httpx.Response(200, json=_company_payload()),
    )

    async with EdinetDbClient(
        api_key="k",
        throttle_seconds=0,
        quota_tracker=tracker,
    ) as client:
        await client.list_companies(page=1, per_page=1)

    assert tracker.requests_today() == 1


@pytest.mark.asyncio
@respx.mock
async def test_three_successful_fetches_record_three_requests(tmp_path: Path) -> None:
    tracker = DailyQuotaTracker(db_path=tmp_path / "q.db", daily_cap=100)
    respx.get(f"{_BASE}/companies").mock(
        return_value=httpx.Response(200, json=_company_payload()),
    )

    async with EdinetDbClient(
        api_key="k",
        throttle_seconds=0,
        quota_tracker=tracker,
    ) as client:
        await client.list_companies(page=1, per_page=1)
        await client.list_companies(page=2, per_page=1)
        await client.list_companies(page=3, per_page=1)

    assert tracker.requests_today() == 3


# === Cache hits don't consume quota ===


@pytest.mark.asyncio
@respx.mock
async def test_cache_hits_do_not_count_against_quota(tmp_path: Path) -> None:
    """The whole point of the cache + quota pair: prefetch fills the
    cache once (counts), subsequent reads hit the cache (don't
    count). Lets a daily prefetch script burn a few requests up
    front then serve thousands of reads from the cache for free."""
    tracker = DailyQuotaTracker(db_path=tmp_path / "q.db", daily_cap=100)
    cache = EdinetDbCache(db_path=tmp_path / "cache.db")
    respx.get(f"{_BASE}/companies").mock(
        return_value=httpx.Response(200, json=_company_payload()),
    )

    async with EdinetDbClient(
        api_key="k",
        throttle_seconds=0,
        cache=cache,
        quota_tracker=tracker,
    ) as client:
        await client.list_companies(page=1, per_page=1)  # HTTP — counts
        # Same args → cache hit, no HTTP, no count.
        await client.list_companies(page=1, per_page=1)
        await client.list_companies(page=1, per_page=1)
        await client.list_companies(page=1, per_page=1)

    assert tracker.requests_today() == 1


# === Pre-exhausted tracker blocks further HTTP ===


@pytest.mark.asyncio
async def test_quota_exhausted_blocks_http_before_dispatch(tmp_path: Path) -> None:
    """Pre-fill the tracker to the cap. The next call must raise
    :class:`EdinetDbQuotaExhaustedError` before touching httpx, so a
    misbehaving caller can't accidentally exceed the quota in a
    tight loop after the budget has run out."""
    tracker = DailyQuotaTracker(db_path=tmp_path / "q.db", daily_cap=5)
    # Bump the count to the cap (5).
    for _ in range(5):
        tracker.record_request()

    async with EdinetDbClient(
        api_key="k",
        throttle_seconds=0,
        quota_tracker=tracker,
    ) as client:
        with pytest.raises(EdinetDbQuotaExhaustedError, match="5/5"):
            await client.list_companies(page=1, per_page=1)


@pytest.mark.asyncio
@respx.mock
async def test_quota_remaining_after_one_call_is_cap_minus_one(tmp_path: Path) -> None:
    tracker = DailyQuotaTracker(db_path=tmp_path / "q.db", daily_cap=10)
    respx.get(f"{_BASE}/companies").mock(
        return_value=httpx.Response(200, json=_company_payload()),
    )

    async with EdinetDbClient(
        api_key="k",
        throttle_seconds=0,
        quota_tracker=tracker,
    ) as client:
        await client.list_companies(page=1, per_page=1)

    assert tracker.quota_remaining() == 9


# === Failed HTTP responses don't count ===


@pytest.mark.asyncio
@respx.mock
async def test_401_response_does_not_consume_quota(tmp_path: Path) -> None:
    """A 401 means we never got useful data; charging quota for it
    would punish callers for invalid-key configuration. The tracker
    only records *successful* responses (Edinet DB's own quota
    accounting may differ, but we err on the side of optimism for
    our own tracker)."""
    tracker = DailyQuotaTracker(db_path=tmp_path / "q.db", daily_cap=100)
    respx.get(f"{_BASE}/companies").mock(
        return_value=httpx.Response(
            401,
            json={
                "statusCode": 401,
                "message": "Access denied due to invalid subscription key.",
            },
        ),
    )

    async with EdinetDbClient(
        api_key="bad",
        throttle_seconds=0,
        quota_tracker=tracker,
    ) as client:
        with pytest.raises(EdinetDbError):
            await client.list_companies(page=1, per_page=1)

    assert tracker.requests_today() == 0
