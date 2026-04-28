"""Polymarket clients honour an injected AsyncRateLimiter.

The default rate-limiter has min_interval_seconds=0 so existing
callers see no per-second pacing (Polymarket's market-data limits
are generous — ~1.5k req / 10s for the CLOB and similar for Gamma —
and a typical Observer cycle is well below that). Batch back-fills
or shared coordination across multiple clients in the same process
inject a paced limiter via the ctor arg.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
import respx

from caqrs.data._common.rate_limit import AsyncRateLimiter
from caqrs.data.polymarket.clob_client import PolymarketClobClient
from caqrs.data.polymarket.gamma_client import PolymarketGammaClient

_CLOB_BASE = "https://clob.polymarket.com"
_GAMMA_BASE = "https://gamma-api.polymarket.com"


# === CLOB ===


def test_clob_default_limiter_disables_pacing() -> None:
    client = PolymarketClobClient()
    # Internal attribute by design — checked here only to confirm
    # the public default hasn't drifted.
    assert client._rate_limiter.min_interval_seconds == 0.0


def test_clob_caller_can_inject_paced_limiter() -> None:
    paced = AsyncRateLimiter(min_interval_seconds=0.01)  # 100 req/s
    client = PolymarketClobClient(rate_limiter=paced)
    assert client._rate_limiter is paced


@pytest.mark.asyncio
@respx.mock
async def test_clob_paced_limiter_actually_paces() -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    respx.get(f"{_CLOB_BASE}/midpoint").mock(
        return_value=httpx.Response(200, json={"mid": "0.45"}),
    )

    paced = AsyncRateLimiter(min_interval_seconds=0.5)
    with patch("caqrs.data._common.rate_limit.asyncio.sleep", side_effect=fake_sleep):
        async with PolymarketClobClient(rate_limiter=paced) as clob:
            await clob.get_midpoint(token_id="abc")
            await clob.get_midpoint(token_id="abc")

    # First call: no sleep. Second call: ~0.5s sleep.
    assert len([s for s in sleeps if s > 0]) == 1
    assert 0.4 <= max(sleeps) <= 0.5


# === Gamma ===


def test_gamma_default_limiter_disables_pacing() -> None:
    client = PolymarketGammaClient()
    assert client._rate_limiter.min_interval_seconds == 0.0


def test_gamma_caller_can_inject_paced_limiter() -> None:
    paced = AsyncRateLimiter(min_interval_seconds=0.01)
    client = PolymarketGammaClient(rate_limiter=paced)
    assert client._rate_limiter is paced


@pytest.mark.asyncio
@respx.mock
async def test_gamma_paced_limiter_actually_paces() -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    respx.get(f"{_GAMMA_BASE}/markets").mock(
        return_value=httpx.Response(200, json=[]),
    )

    paced = AsyncRateLimiter(min_interval_seconds=0.5)
    with patch("caqrs.data._common.rate_limit.asyncio.sleep", side_effect=fake_sleep):
        async with PolymarketGammaClient(rate_limiter=paced) as gamma:
            await gamma.list_markets(slugs=("test",))
            await gamma.list_markets(slugs=("test",))

    assert len([s for s in sleeps if s > 0]) == 1
    assert 0.4 <= max(sleeps) <= 0.5


# === Independent limiters → no cross-client pacing by default ===


@pytest.mark.asyncio
@respx.mock
async def test_independent_limiters_do_not_pace_each_other() -> None:
    """Two clients with default limiters share no state. Required so
    one batch back-fill on Gamma does not slow down a concurrent
    CLOB poll."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    respx.get(f"{_CLOB_BASE}/midpoint").mock(
        return_value=httpx.Response(200, json={"mid": "0.45"}),
    )
    respx.get(f"{_GAMMA_BASE}/markets").mock(
        return_value=httpx.Response(200, json=[]),
    )

    with patch("caqrs.data._common.rate_limit.asyncio.sleep", side_effect=fake_sleep):
        async with (
            PolymarketClobClient() as clob,
            PolymarketGammaClient() as gamma,
        ):
            await clob.get_midpoint(token_id="abc")
            await gamma.list_markets(slugs=("test",))
            await clob.get_midpoint(token_id="abc")
            await gamma.list_markets(slugs=("test",))

    # All four calls execute without artificial pacing.
    assert all(s == 0 for s in sleeps)


@pytest.mark.asyncio
@respx.mock
async def test_shared_limiter_paces_across_clients() -> None:
    """Conversely, when callers share one limiter (e.g. to coordinate
    a single upstream account across CLOB + Gamma), pacing applies
    across both."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    respx.get(f"{_CLOB_BASE}/midpoint").mock(
        return_value=httpx.Response(200, json={"mid": "0.45"}),
    )
    respx.get(f"{_GAMMA_BASE}/markets").mock(
        return_value=httpx.Response(200, json=[]),
    )

    shared = AsyncRateLimiter(min_interval_seconds=0.3)
    with patch("caqrs.data._common.rate_limit.asyncio.sleep", side_effect=fake_sleep):
        async with (
            PolymarketClobClient(rate_limiter=shared) as clob,
            PolymarketGammaClient(rate_limiter=shared) as gamma,
        ):
            await clob.get_midpoint(token_id="abc")
            await gamma.list_markets(slugs=("test",))  # paced by CLOB call

    # 1 call from CLOB (no pre-pace), 1 from Gamma (paced because
    # CLOB call recorded a timestamp on the shared limiter).
    paced_sleeps = [s for s in sleeps if s > 0]
    assert len(paced_sleeps) == 1
    assert 0.2 <= paced_sleeps[0] <= 0.3
