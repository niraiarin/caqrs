"""AsyncRateLimiter — shared throttling utility for data clients.

Originally factored out of yfinance's client.py for reuse across
J-Quants (free tier 5 req/min → 12s interval) and Polymarket (10
req/sec → 0.1s). The limiter enforces a minimum interval between
acquires + tracks consecutive 429s for linear backoff.

Tests use ``patch("asyncio.sleep")`` so wall-clock waits don't slow
the suite; assertions inspect the captured sleep durations directly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

from caqrs.data._common.rate_limit import AsyncRateLimiter


@asynccontextmanager
async def _record_sleeps() -> AsyncIterator[list[float]]:
    """Yields a list that captures every ``asyncio.sleep`` duration."""
    captured: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        captured.append(seconds)

    with patch("asyncio.sleep", side_effect=fake_sleep):
        yield captured


# === Min-interval pacing ===


@pytest.mark.asyncio
async def test_first_acquire_does_not_sleep() -> None:
    """No previous call → no waiting required."""
    limiter = AsyncRateLimiter(min_interval_seconds=1.0)
    async with _record_sleeps() as sleeps:
        await limiter.acquire()
    assert sleeps == []


@pytest.mark.asyncio
async def test_second_acquire_waits_for_min_interval() -> None:
    """When called immediately after the previous acquire, the
    limiter sleeps the full interval minus elapsed (which is ~0)."""
    limiter = AsyncRateLimiter(min_interval_seconds=1.0)
    async with _record_sleeps() as sleeps:
        await limiter.acquire()
        await limiter.acquire()
    # First acquire: no sleep. Second acquire: ~1s sleep (allow tiny
    # tolerance for the trivially-elapsed time between calls).
    assert len(sleeps) == 1
    assert 0.9 <= sleeps[0] <= 1.0


@pytest.mark.asyncio
async def test_zero_interval_disables_pacing() -> None:
    """``min_interval_seconds=0`` means no inter-call waiting — useful
    for tests that need to fire requests in rapid succession."""
    limiter = AsyncRateLimiter(min_interval_seconds=0.0)
    async with _record_sleeps() as sleeps:
        for _ in range(5):
            await limiter.acquire()
    assert sleeps == []


# === 429 backoff ===


@pytest.mark.asyncio
async def test_on_429_returns_next_backoff() -> None:
    """First 429 → first slot in the schedule; second 429 → second; …"""
    limiter = AsyncRateLimiter(min_interval_seconds=0.0, retry_schedule=(30, 60, 90))
    assert limiter.on_429() == 30
    assert limiter.on_429() == 60
    assert limiter.on_429() == 90


@pytest.mark.asyncio
async def test_on_429_caps_at_last_schedule_value() -> None:
    """After the schedule is exhausted, callers can still query for a
    backoff (rate-limited beyond the modelled budget); we cap at the
    last value rather than raise — graceful degradation."""
    limiter = AsyncRateLimiter(min_interval_seconds=0.0, retry_schedule=(30, 60))
    limiter.on_429()
    limiter.on_429()
    # 3rd 429 — cap at 60.
    assert limiter.on_429() == 60


@pytest.mark.asyncio
async def test_reset_429_starts_schedule_over() -> None:
    """A successful response after a streak of 429s resets the
    counter so the next 429 (whenever it comes) starts at slot 0
    again."""
    limiter = AsyncRateLimiter(min_interval_seconds=0.0, retry_schedule=(30, 60))
    limiter.on_429()
    limiter.on_429()
    limiter.reset_429()
    assert limiter.on_429() == 30


# === Defaults ===


def test_default_retry_schedule_is_30_60_90() -> None:
    """Per Zenn ino_h article: J-Quants needs linear (not exponential)
    backoff because the rate-limit window is fixed at 1 minute. 5/10/
    15s exponential is too short."""
    limiter = AsyncRateLimiter(min_interval_seconds=1.0)
    assert limiter.retry_schedule == (30, 60, 90)


# === Composability: shared limiter across clients ===


@pytest.mark.asyncio
async def test_shared_limiter_paces_acquires_globally() -> None:
    """Two callers using the same limiter share the timestamp clock:
    a second acquire from caller B (right after caller A's) waits
    the same interval. That's the whole point of factoring this out
    of yfinance — multiple clients hitting the same upstream can
    coordinate."""
    limiter = AsyncRateLimiter(min_interval_seconds=1.0)
    async with _record_sleeps() as sleeps:
        await limiter.acquire()  # caller A
        await limiter.acquire()  # caller B (shared limiter)
    assert len(sleeps) == 1
