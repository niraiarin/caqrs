"""yFinance → AssetSnapshot bridge.

Mirrors the J-Quants observer_signals helper exactly: same
``AssetSnapshot`` shape, same metric definitions (21-day return,
252-day return, 30-day sample stdev), same "insufficient history →
None" semantics. The agent receives the snapshot and can't tell
which data source produced it — that's the whole point of the
abstraction.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pandas as pd
import pytest

from caqrs.data.yfinance.client import YFinanceClient
from caqrs.data.yfinance.observer_signals import fetch_yfinance_asset_snapshot


def _frame(dates: list[str], closes: list[float]) -> pd.DataFrame:
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


@pytest.mark.asyncio
async def test_snapshot_carries_ticker_and_last_close() -> None:
    frame = _frame(
        dates=[f"2026-04-{d:02d}" for d in range(1, 11)],
        closes=[100.0 + i for i in range(10)],
    )
    with patch("caqrs.data.yfinance.client.yf") as yf_mock:
        yf_mock.Ticker.return_value.history.return_value = frame
        async with YFinanceClient() as client:
            snapshot = await fetch_yfinance_asset_snapshot(
                client=client,
                symbol="AAPL",
            )

    assert snapshot.ticker == "AAPL"
    assert snapshot.last_close == Decimal("109")


@pytest.mark.asyncio
async def test_returns_none_for_short_history_metrics() -> None:
    """Insufficient history degrades the relevant metric to None
    rather than zero — same convention as J-Quants helper."""
    # Only 5 days — no 21d, no 252d, no 30d-stdev.
    frame = _frame(
        dates=[f"2026-04-{d:02d}" for d in range(1, 6)],
        closes=[100.0, 101.0, 102.0, 103.0, 104.0],
    )
    with patch("caqrs.data.yfinance.client.yf") as yf_mock:
        yf_mock.Ticker.return_value.history.return_value = frame
        async with YFinanceClient() as client:
            snapshot = await fetch_yfinance_asset_snapshot(
                client=client,
                symbol="AAPL",
            )

    assert snapshot.return_1m is None
    assert snapshot.return_12m is None
    assert snapshot.volatility_30d is None


@pytest.mark.asyncio
async def test_computes_21day_return_when_history_sufficient() -> None:
    # 22 days at +1 per day → return_1m = (121 - 100) / 100 = 0.21.
    frame = _frame(
        dates=[f"2026-04-{d:02d}" for d in range(1, 23)],
        closes=[100.0 + i for i in range(22)],
    )
    with patch("caqrs.data.yfinance.client.yf") as yf_mock:
        yf_mock.Ticker.return_value.history.return_value = frame
        async with YFinanceClient() as client:
            snapshot = await fetch_yfinance_asset_snapshot(
                client=client,
                symbol="AAPL",
            )

    assert snapshot.return_1m is not None
    # Compare with tolerance — Decimal arithmetic on floats has noise.
    assert abs(snapshot.return_1m - Decimal("0.21")) < Decimal("0.001")
