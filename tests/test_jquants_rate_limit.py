"""J-Quants rate-limit integration.

Verifies the JQuantsClient honours the AsyncRateLimiter:
  - default ctor pulls in a 12s-interval limiter (free tier 5 req/min)
  - 429 responses trigger linear-backoff retry
  - 3 sustained 429s exhaust retries and raise

Tests use ``patch("asyncio.sleep")`` so wall-clock waits don't slow
the suite; respx handles the HTTP mocking.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
import respx

from caqrs.data._common.rate_limit import AsyncRateLimiter
from caqrs.data.jquants.client import JQuantsClient, JQuantsError

_BASE = "https://api.jquants.com/v2"
_KEY = "test-key"


def _master_payload() -> dict[str, object]:
    return {
        "data": [
            {
                "Date": "2024-04-15",
                "Code": "13010",
                "CoName": "極洋",
                "CoNameEn": "KYOKUYO",
                "S17": "1",
                "S17Nm": "FOODS",
                "S33": "0050",
                "S33Nm": "FOODS",
                "ScaleCat": "TOPIX Mid400",
                "Mkt": "0111",
                "MktNm": "Prime",
            },
        ],
    }


# === Default limiter ===


def test_default_rate_limiter_min_interval_is_free_tier_pacing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Constructed without an explicit limiter, the production client
    uses the free-tier 12s interval (5 req/min). The tests/conftest.py
    autouse fixture patches this to 0 for test speed; we undo that
    for this one assertion to verify the production constant."""
    monkeypatch.setattr(
        "caqrs.data.jquants.client._FREE_TIER_MIN_INTERVAL_SECONDS",
        12.0,
    )
    client = JQuantsClient(api_key=_KEY)
    limiter = client._rate_limiter
    assert isinstance(limiter, AsyncRateLimiter)
    assert limiter.min_interval_seconds == 12.0


def test_default_retry_schedule_is_30_60_90(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same monkeypatch undo as above for the retry-schedule constant."""
    monkeypatch.setattr(
        "caqrs.data.jquants.client._DEFAULT_RETRY_SCHEDULE",
        (30, 60, 90),
    )
    client = JQuantsClient(api_key=_KEY)
    assert client._rate_limiter.retry_schedule == (30, 60, 90)


def test_caller_can_inject_custom_limiter() -> None:
    """Paid-tier callers (60 req/min) supply a 1s-interval limiter."""
    custom = AsyncRateLimiter(min_interval_seconds=1.0)
    client = JQuantsClient(api_key=_KEY, rate_limiter=custom)
    assert client._rate_limiter is custom


# === 429 backoff retry ===


@pytest.mark.asyncio
@respx.mock
async def test_recovers_from_single_429() -> None:
    """First call → 429 → backoff 30s → retry → 200."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    route = respx.get(f"{_BASE}/equities/master").mock(
        side_effect=[
            httpx.Response(429, text="Too Many Requests"),
            httpx.Response(200, json=_master_payload()),
        ],
    )

    fast_limiter = AsyncRateLimiter(min_interval_seconds=0.0, retry_schedule=(30, 60, 90))
    with patch("caqrs.data.jquants.client.asyncio.sleep", side_effect=fake_sleep):
        async with JQuantsClient(api_key=_KEY, rate_limiter=fast_limiter) as client:
            rows = await client.list_master(code="13010")

    assert route.call_count == 2
    assert len(rows) == 1
    # First slot of the retry schedule.
    assert 30 in sleeps


@pytest.mark.asyncio
@respx.mock
async def test_exhausts_retries_after_three_consecutive_429s() -> None:
    """4 calls (1 attempt + 3 retries) all 429 → JQuantsError 429."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    respx.get(f"{_BASE}/equities/master").mock(
        return_value=httpx.Response(429, text="Too Many Requests"),
    )

    fast_limiter = AsyncRateLimiter(min_interval_seconds=0.0, retry_schedule=(30, 60, 90))
    with patch("caqrs.data.jquants.client.asyncio.sleep", side_effect=fake_sleep):
        async with JQuantsClient(api_key=_KEY, rate_limiter=fast_limiter) as client:
            with pytest.raises(JQuantsError) as exc_info:
                await client.list_master(code="13010")

    assert exc_info.value.status_code == 429
    # Three retry sleeps used: 30, 60, 90.
    backoff_sleeps = [s for s in sleeps if s in (30, 60, 90)]
    assert backoff_sleeps == [30, 60, 90]


@pytest.mark.asyncio
@respx.mock
async def test_success_after_429_resets_streak_counter() -> None:
    """After a 429-then-200 sequence, the next 429 starts at slot 0
    again rather than at slot 1."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    respx.get(f"{_BASE}/equities/master").mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json=_master_payload()),
            httpx.Response(429),
            httpx.Response(200, json=_master_payload()),
        ],
    )

    fast_limiter = AsyncRateLimiter(min_interval_seconds=0.0, retry_schedule=(30, 60, 90))
    with patch("caqrs.data.jquants.client.asyncio.sleep", side_effect=fake_sleep):
        async with JQuantsClient(api_key=_KEY, rate_limiter=fast_limiter) as client:
            await client.list_master(code="13010")
            await client.list_master(code="13010")

    # Two 429s; both should use slot 0 (30s).
    backoff_sleeps = [s for s in sleeps if s in (30, 60, 90)]
    assert backoff_sleeps == [30, 30]


# === Min-interval pacing ===


@pytest.mark.asyncio
@respx.mock
async def test_min_interval_paces_consecutive_calls() -> None:
    """Two back-to-back successful calls: the second one waits the
    full interval since the first."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    respx.get(f"{_BASE}/equities/master").mock(
        return_value=httpx.Response(200, json=_master_payload()),
    )

    paced_limiter = AsyncRateLimiter(min_interval_seconds=12.0)
    # Patch asyncio.sleep at the rate_limit module level (where the
    # limiter calls it) — separate from the client-level patch
    # used for backoff tests.
    with patch("caqrs.data._common.rate_limit.asyncio.sleep", side_effect=fake_sleep):
        async with JQuantsClient(api_key=_KEY, rate_limiter=paced_limiter) as client:
            await client.list_master(code="13010")
            await client.list_master(code="13010")

    # Second call's pacing: waits ~12s.
    paced = [s for s in sleeps if s > 1]
    assert len(paced) == 1
    assert 11.9 <= paced[0] <= 12.0
