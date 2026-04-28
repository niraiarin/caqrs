"""yFinance client behaviour.

The yfinance library is sync, scrapes Yahoo's HTML, and silently
returns empty DataFrames when rate-limited (Zenn yfinance-production-
pitfalls). This client wraps it in three production safeguards:

1. **Async via** ``asyncio.to_thread`` — keeps CAQRS's async-first
   convention and avoids blocking the event loop.
2. **Per-process tz cache** via ``tempfile.mkdtemp`` —
   ``~/.cache/py-yfinance`` SQLite contention bites when multiple
   cycles run concurrently.
3. **Explicit rate-limit detection** — distinguishes "no data for
   this period" from "rate limited" so a quiet failure mode becomes
   a typed, retryable error.

Every test mocks the ``yfinance`` library at the boundary; nothing
here actually hits Yahoo.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from caqrs.data.yfinance.client import (
    YFinanceClient,
    YFinanceQuotaExhaustedError,
)

# === Fixture builders ===


def _ohlcv_frame(
    *,
    dates: list[str],
    closes: list[float],
) -> pd.DataFrame:
    """Build a yfinance-shaped daily-bar DataFrame."""
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


# === daily_bars ===


@pytest.mark.asyncio
async def test_daily_bars_returns_typed_records() -> None:
    frame = _ohlcv_frame(
        dates=["2026-04-25", "2026-04-26", "2026-04-27"],
        closes=[100.0, 101.0, 102.5],
    )
    with patch("caqrs.data.yfinance.client.yf") as yf_mock:
        yf_mock.Ticker.return_value.history.return_value = frame
        async with YFinanceClient() as client:
            bars = await client.daily_bars(
                symbol="AAPL",
                from_date=date(2026, 4, 25),
                to_date=date(2026, 4, 27),
            )

    assert len(bars) == 3
    assert bars[0].symbol == "AAPL"
    assert bars[0].date == date(2026, 4, 25)
    assert bars[0].close == Decimal("100")
    assert bars[2].close == Decimal("102.5")


@pytest.mark.asyncio
async def test_daily_bars_passes_auto_adjust_true() -> None:
    """auto_adjust=True is non-negotiable per Zenn pitfalls article —
    omit it and prices have split discontinuities."""
    frame = _ohlcv_frame(dates=["2026-04-25"], closes=[100.0])
    history_mock = MagicMock(return_value=frame)
    with patch("caqrs.data.yfinance.client.yf") as yf_mock:
        yf_mock.Ticker.return_value.history = history_mock
        async with YFinanceClient() as client:
            await client.daily_bars(
                symbol="AAPL",
                from_date=date(2026, 4, 25),
                to_date=date(2026, 4, 25),
            )

    _, kwargs = history_mock.call_args
    assert kwargs["auto_adjust"] is True


@pytest.mark.asyncio
async def test_daily_bars_distinguishes_no_data_from_rate_limit() -> None:
    """Empty DataFrame is ambiguous: yfinance returns it both for "no
    bars in range" AND for rate-limited / blocked responses. Without
    further signal, the client treats consecutive empties as
    rate-limiting (3 in a row → quota_exhausted)."""
    empty = pd.DataFrame()
    with patch("caqrs.data.yfinance.client.yf") as yf_mock:
        yf_mock.Ticker.return_value.history.return_value = empty
        async with YFinanceClient(throttle_seconds=0) as client:
            # Single empty: returns [], not raise — could legitimately be
            # "no trading days in range".
            bars = await client.daily_bars(
                symbol="AAPL",
                from_date=date(2026, 4, 25),
                to_date=date(2026, 4, 27),
            )
            assert bars == []


@pytest.mark.asyncio
async def test_three_consecutive_empties_trip_quota_exhausted() -> None:
    """Three calls in a row returning empty → treat as rate-limited.
    Mirrors Zenn rakuscan article's _quota_exhausted heuristic."""
    empty = pd.DataFrame()
    with patch("caqrs.data.yfinance.client.yf") as yf_mock:
        yf_mock.Ticker.return_value.history.return_value = empty
        async with YFinanceClient(throttle_seconds=0) as client:
            # First 2 empties — no error yet.
            await client.daily_bars(
                symbol="AAA",
                from_date=date(2026, 4, 25),
                to_date=date(2026, 4, 27),
            )
            await client.daily_bars(
                symbol="BBB",
                from_date=date(2026, 4, 25),
                to_date=date(2026, 4, 27),
            )
            # 3rd empty → quota exhausted.
            with pytest.raises(YFinanceQuotaExhaustedError, match="3 consecutive"):
                await client.daily_bars(
                    symbol="CCC",
                    from_date=date(2026, 4, 25),
                    to_date=date(2026, 4, 27),
                )


@pytest.mark.asyncio
async def test_non_empty_response_resets_consecutive_empty_counter() -> None:
    """A successful response in the middle of empties resets the
    counter — only sustained empties trip quota detection."""
    empty = pd.DataFrame()
    good = _ohlcv_frame(dates=["2026-04-26"], closes=[101.0])

    history_mock = MagicMock(side_effect=[empty, empty, good, empty, empty])
    with patch("caqrs.data.yfinance.client.yf") as yf_mock:
        yf_mock.Ticker.return_value.history = history_mock
        async with YFinanceClient(throttle_seconds=0) as client:
            for sym in ("AAA", "BBB", "CCC", "DDD", "EEE"):
                # None of the 5 should raise — the success at position
                # 3 resets the counter, so we never hit 3-in-a-row
                # empties.
                await client.daily_bars(
                    symbol=sym,
                    from_date=date(2026, 4, 25),
                    to_date=date(2026, 4, 27),
                )


@pytest.mark.asyncio
async def test_daily_bars_coerces_nan_volume_to_none() -> None:
    """yfinance returns NaN for missing volume; Decimal/int coercion
    fails on NaN, so the client must convert to None first."""
    frame = pd.DataFrame(
        {
            "Open": [99.0],
            "High": [101.0],
            "Low": [98.0],
            "Close": [100.0],
            "Volume": [float("nan")],
        },
        index=pd.DatetimeIndex(["2026-04-25"], name="Date"),
    )
    with patch("caqrs.data.yfinance.client.yf") as yf_mock:
        yf_mock.Ticker.return_value.history.return_value = frame
        async with YFinanceClient() as client:
            bars = await client.daily_bars(
                symbol="AAPL",
                from_date=date(2026, 4, 25),
                to_date=date(2026, 4, 25),
            )

    assert bars[0].volume is None


# === Per-process tz cache ===


@pytest.mark.asyncio
async def test_client_isolates_tz_cache_per_process() -> None:
    """Per-process tempdir for tz cache prevents the SQLite contention
    documented in Zenn yfinance-production-pitfalls."""
    set_tz_mock = MagicMock()
    with patch("caqrs.data.yfinance.client.yf") as yf_mock:
        yf_mock.set_tz_cache_location = set_tz_mock
        yf_mock.Ticker.return_value.history.return_value = pd.DataFrame()
        async with YFinanceClient(throttle_seconds=0) as client:
            assert client.tz_cache_dir is not None
        # Every constructed client gets its own dir.
        set_tz_mock.assert_called_once()
        assert "/tmp" in str(set_tz_mock.call_args[0][0]) or "T/" in str(
            set_tz_mock.call_args[0][0],
        )


@pytest.mark.asyncio
async def test_aclose_cleans_up_tz_cache() -> None:
    """The tempdir is removed on close — long-running supervisors
    spawn many clients and leaked dirs accumulate."""
    with patch("caqrs.data.yfinance.client.yf") as yf_mock:
        yf_mock.Ticker.return_value.history.return_value = pd.DataFrame()
        client = YFinanceClient()
        cache_dir = client.tz_cache_dir
        assert cache_dir is not None
        assert cache_dir.exists()
        await client.aclose()
        assert not cache_dir.exists()
