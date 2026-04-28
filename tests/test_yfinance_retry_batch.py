"""Linear-backoff retry + batch fetch — Zenn pitfalls alignment.

The retry helper handles the "we just hit quota; let's wait and try
again" pattern documented in both Zenn articles. Linear backoff
(5s -> 10s -> 15s) before each retry; max 3 attempts total before
re-raising. The empty-counter is not reset between retries — a
non-empty response from daily_bars itself resets it.

The batch helper sequences N symbols at the configured throttle
interval (default 1.0s) and short-circuits on quota exhaustion so
the supervisor gets a partial dict + the error rather than losing
everything.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from caqrs.data.yfinance.cache import YFinanceCache
from caqrs.data.yfinance.client import (
    YFinanceClient,
    YFinanceQuotaExhaustedError,
)
from caqrs.data.yfinance.schemas import YFinancePrice


def _ohlcv(dates: list[str], closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": [c - 1 for c in closes],
            "High": [c + 1 for c in closes],
            "Low": [c - 2 for c in closes],
            "Close": closes,
            "Volume": [1_000_000] * len(closes),
        },
        index=pd.DatetimeIndex(dates, name="Date"),
    )


# === Retry helper ===


@pytest.mark.asyncio
async def test_retry_recovers_after_quota_event() -> None:
    """Client primed near the quota threshold then the helper hits
    it; backoff + retry succeeds with the next frame."""
    empty = pd.DataFrame()
    good = _ohlcv(["2026-04-25"], [100.0])

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    history_mock = MagicMock(side_effect=[empty, empty, empty, good])
    with (
        patch("caqrs.data.yfinance.client.yf") as yf_mock,
        patch("asyncio.sleep", side_effect=fake_sleep),
    ):
        yf_mock.Ticker.return_value.history = history_mock
        async with YFinanceClient(throttle_seconds=0) as client:
            # Prime the counter to 2 with two plain daily_bars calls
            # that return empty (count 1, count 2 — neither raises).
            await client.daily_bars(
                symbol="AAA",
                from_date=date(2026, 4, 25),
                to_date=date(2026, 4, 25),
            )
            await client.daily_bars(
                symbol="BBB",
                from_date=date(2026, 4, 25),
                to_date=date(2026, 4, 25),
            )
            # Third empty -> quota -> retry resets counter -> next
            # attempt gets `good`.
            bars = await client.daily_bars_with_retry(
                symbol="CCC",
                from_date=date(2026, 4, 25),
                to_date=date(2026, 4, 25),
            )

    assert len(bars) == 1
    # First retry waits 5s.
    assert 5 in sleep_calls


@pytest.mark.asyncio
async def test_retry_gives_up_after_max_attempts() -> None:
    """Sustained empties exhaust all 3 retries with 5/10/15s waits."""
    empty = pd.DataFrame()

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    with (
        patch("caqrs.data.yfinance.client.yf") as yf_mock,
        patch("asyncio.sleep", side_effect=fake_sleep),
    ):
        yf_mock.Ticker.return_value.history.return_value = empty
        async with YFinanceClient(throttle_seconds=0) as client:
            # Prime to 2 so the first retry attempt hits quota.
            await client.daily_bars(
                symbol="AAA",
                from_date=date(2026, 4, 25),
                to_date=date(2026, 4, 25),
            )
            await client.daily_bars(
                symbol="BBB",
                from_date=date(2026, 4, 25),
                to_date=date(2026, 4, 25),
            )
            with pytest.raises(YFinanceQuotaExhaustedError):
                await client.daily_bars_with_retry(
                    symbol="CCC",
                    from_date=date(2026, 4, 25),
                    to_date=date(2026, 4, 25),
                )

    backoff_sleeps = [s for s in sleep_calls if s in (5, 10, 15)]
    assert backoff_sleeps == [5, 10, 15]


# === Batch helper ===


@pytest.mark.asyncio
async def test_batch_fetch_returns_dict_keyed_by_symbol() -> None:
    frame_a = _ohlcv(["2026-04-25"], [100.0])
    frame_b = _ohlcv(["2026-04-25"], [200.0])

    history_mock = MagicMock(side_effect=[frame_a, frame_b])
    with patch("caqrs.data.yfinance.client.yf") as yf_mock:
        yf_mock.Ticker.return_value.history = history_mock
        async with YFinanceClient(throttle_seconds=0) as client:
            results = await client.daily_bars_batch(
                symbols=("AAPL", "MSFT"),
                from_date=date(2026, 4, 25),
                to_date=date(2026, 4, 25),
            )

    assert set(results) == {"AAPL", "MSFT"}
    assert results["AAPL"][0].close.is_finite()
    assert results["MSFT"][0].close.is_finite()


@pytest.mark.asyncio
async def test_batch_fetch_propagates_quota_after_partial_collection() -> None:
    """If symbol N fails with quota exhaustion, the batch raises but
    the caller can recover the partial dict via the exception's
    .partial attribute."""
    good = _ohlcv(["2026-04-25"], [100.0])
    empty = pd.DataFrame()

    history_mock = MagicMock(side_effect=[good, empty, empty, empty])

    with patch("caqrs.data.yfinance.client.yf") as yf_mock:
        yf_mock.Ticker.return_value.history = history_mock
        async with YFinanceClient(throttle_seconds=0) as client:
            with pytest.raises(YFinanceQuotaExhaustedError) as exc_info:
                await client.daily_bars_batch(
                    symbols=("AAPL", "MSFT", "NVDA", "GOOG"),
                    from_date=date(2026, 4, 25),
                    to_date=date(2026, 4, 25),
                )
            partial = getattr(exc_info.value, "partial", {})
            assert "AAPL" in partial
            assert "GOOG" not in partial


@pytest.mark.asyncio
async def test_batch_fetch_uses_cache_on_hit(tmp_path: Path) -> None:
    cache_path = tmp_path / "yf-cache.db"
    cache = YFinanceCache(db_path=cache_path)
    cached_bars = (
        YFinancePrice(
            symbol="AAPL",
            date=date(2026, 4, 25),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            adjusted_close=None,
            volume=1_000_000,
        ),
    )
    cache.set_bars(
        symbol="AAPL",
        from_date=date(2026, 4, 25),
        to_date=date(2026, 4, 25),
        bars=cached_bars,
        ttl_seconds=86_400,
    )

    history_mock = MagicMock()
    with patch("caqrs.data.yfinance.client.yf") as yf_mock:
        yf_mock.Ticker.return_value.history = history_mock
        async with YFinanceClient(throttle_seconds=0, cache=cache) as client:
            results = await client.daily_bars_batch(
                symbols=("AAPL",),
                from_date=date(2026, 4, 25),
                to_date=date(2026, 4, 25),
            )

    assert results["AAPL"] == list(cached_bars)
    history_mock.assert_not_called()


# === Cache wired through daily_bars (single call) ===


@pytest.mark.asyncio
async def test_daily_bars_writes_to_cache_on_fresh_fetch(tmp_path: Path) -> None:
    """First call hits Yahoo, second call hits cache."""
    cache_path = tmp_path / "yf-cache.db"
    cache = YFinanceCache(db_path=cache_path)

    frame = _ohlcv(["2026-04-25"], [100.0])
    history_mock = MagicMock(return_value=frame)

    with patch("caqrs.data.yfinance.client.yf") as yf_mock:
        yf_mock.Ticker.return_value.history = history_mock
        async with YFinanceClient(throttle_seconds=0, cache=cache) as client:
            first = await client.daily_bars(
                symbol="AAPL",
                from_date=date(2026, 4, 25),
                to_date=date(2026, 4, 25),
            )
            second = await client.daily_bars(
                symbol="AAPL",
                from_date=date(2026, 4, 25),
                to_date=date(2026, 4, 25),
            )

    assert first == second
    assert history_mock.call_count == 1
