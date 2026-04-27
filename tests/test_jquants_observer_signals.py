"""Tests for fetch_jquants_asset_snapshot — JQuantsClient -> AssetSnapshot."""

from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from math import isclose

import httpx
import pytest
import respx

from caqrs.data.jquants import JQuantsClient
from caqrs.data.jquants.observer_signals import (
    fetch_jquants_asset_snapshot,
)
from caqrs.schemas.observer import AssetSnapshot

_BASE = "https://api.jquants.com/v2"
_KEY = "jq-test-key"


def _bars_payload(records: Iterable[dict[str, object]]) -> dict[str, object]:
    return {"data": list(records)}


def _bar(*, day: date, code: str, close: str, vo: int = 1000) -> dict[str, object]:
    """Build a synthetic daily bar with adjusted close == close (no corp actions)."""
    return {
        "Date": day.isoformat(),
        "Code": code,
        "O": close,
        "H": close,
        "L": close,
        "C": close,
        "Vo": vo,
        "Va": str(int(float(close) * vo)),
        "AdjFactor": "1.0",
        "AdjO": close,
        "AdjH": close,
        "AdjL": close,
        "AdjC": close,
        "AdjVo": vo,
    }


def _consecutive_dates(*, end: date, count: int) -> list[date]:
    """Return ``count`` dates ending at ``end``, oldest first.

    The helper indexes bars by position, not calendar arithmetic, so
    weekend handling is irrelevant for unit testing — a contiguous
    range of any kind works.
    """
    return [date.fromordinal(end.toordinal() - (count - 1 - i)) for i in range(count)]


@pytest.mark.asyncio
@respx.mock
async def test_snapshot_populates_last_close_and_returns() -> None:
    """1m and 12m returns derive from 21 / 252 trading days back."""
    end = date(2026, 1, 30)
    days = _consecutive_dates(end=end, count=260)
    bars = [_bar(day=d, code="13010", close=str(100 + i * 0.5)) for i, d in enumerate(days)]
    respx.get(f"{_BASE}/equities/bars/daily").mock(
        return_value=httpx.Response(200, json=_bars_payload(bars)),
    )

    async with JQuantsClient(api_key=_KEY) as client:
        snapshot = await fetch_jquants_asset_snapshot(client=client, code="13010")

    assert isinstance(snapshot, AssetSnapshot)
    assert snapshot.ticker == "13010"
    last_close = 100 + 259 * 0.5
    assert snapshot.last_close == Decimal(str(last_close))
    past_1m = 100 + (259 - 21) * 0.5
    expected_return_1m = (last_close - past_1m) / past_1m
    assert snapshot.return_1m is not None
    assert isclose(float(snapshot.return_1m), expected_return_1m, rel_tol=1e-6)
    past_12m = 100 + (259 - 252) * 0.5
    expected_return_12m = (last_close - past_12m) / past_12m
    assert snapshot.return_12m is not None
    assert isclose(float(snapshot.return_12m), expected_return_12m, rel_tol=1e-6)


@pytest.mark.asyncio
@respx.mock
async def test_snapshot_volatility_zero_for_constant_returns() -> None:
    """A geometric series with constant per-day return has zero stdev."""
    end = date(2026, 1, 30)
    days = _consecutive_dates(end=end, count=60)
    prices = [100.0]
    for _ in range(1, 60):
        prices.append(prices[-1] * 1.01)
    bars = [
        _bar(day=d, code="13010", close=str(round(p, 4))) for d, p in zip(days, prices, strict=True)
    ]
    respx.get(f"{_BASE}/equities/bars/daily").mock(
        return_value=httpx.Response(200, json=_bars_payload(bars)),
    )

    async with JQuantsClient(api_key=_KEY) as client:
        snapshot = await fetch_jquants_asset_snapshot(client=client, code="13010")

    assert snapshot.volatility_30d is not None
    assert float(snapshot.volatility_30d) < 1e-6


@pytest.mark.asyncio
@respx.mock
async def test_snapshot_volatility_nonzero_for_alternating_returns() -> None:
    """Alternating ±1% returns produce a sample stdev close to 0.01."""
    end = date(2026, 1, 30)
    days = _consecutive_dates(end=end, count=40)
    prices = [100.0]
    for i in range(1, 40):
        prices.append(prices[-1] * (1.01 if i % 2 == 0 else 0.99))
    bars = [
        _bar(day=d, code="13010", close=str(round(p, 6))) for d, p in zip(days, prices, strict=True)
    ]
    respx.get(f"{_BASE}/equities/bars/daily").mock(
        return_value=httpx.Response(200, json=_bars_payload(bars)),
    )

    async with JQuantsClient(api_key=_KEY) as client:
        snapshot = await fetch_jquants_asset_snapshot(client=client, code="13010")

    assert snapshot.volatility_30d is not None
    expected = 0.01
    assert isclose(float(snapshot.volatility_30d), expected, rel_tol=0.05)


@pytest.mark.asyncio
@respx.mock
async def test_snapshot_short_history_degrades_to_none() -> None:
    """Less than 252 -> return_12m=None; less than 21 -> return_1m=None;
    less than 30 -> volatility_30d=None. last_close is still set."""
    end = date(2026, 1, 30)
    days = _consecutive_dates(end=end, count=10)
    bars = [_bar(day=d, code="13010", close="100.0") for d in days]
    respx.get(f"{_BASE}/equities/bars/daily").mock(
        return_value=httpx.Response(200, json=_bars_payload(bars)),
    )

    async with JQuantsClient(api_key=_KEY) as client:
        snapshot = await fetch_jquants_asset_snapshot(client=client, code="13010")

    assert snapshot.last_close == Decimal("100.0")
    assert snapshot.return_1m is None
    assert snapshot.return_12m is None
    assert snapshot.volatility_30d is None


@pytest.mark.asyncio
@respx.mock
async def test_snapshot_no_bars_raises() -> None:
    """Empty response cannot become a valid snapshot — last_close>0
    invariant on AssetSnapshot would reject it. Raise instead so
    the Observer treats it as a data-quality issue."""
    respx.get(f"{_BASE}/equities/bars/daily").mock(
        return_value=httpx.Response(200, json={"data": []}),
    )

    async with JQuantsClient(api_key=_KEY) as client:
        with pytest.raises(ValueError, match="no daily bars"):
            await fetch_jquants_asset_snapshot(client=client, code="13010")


@pytest.mark.asyncio
@respx.mock
async def test_snapshot_uses_adjusted_close_when_present() -> None:
    """Adjusted close takes precedence over raw close (corporate-action aware)."""
    end = date(2026, 1, 30)
    days = _consecutive_dates(end=end, count=5)
    bars = [{**_bar(day=d, code="13010", close="100.0"), "AdjC": "200.0"} for d in days]
    respx.get(f"{_BASE}/equities/bars/daily").mock(
        return_value=httpx.Response(200, json=_bars_payload(bars)),
    )

    async with JQuantsClient(api_key=_KEY) as client:
        snapshot = await fetch_jquants_asset_snapshot(client=client, code="13010")

    assert snapshot.last_close == Decimal("200.0")


@pytest.mark.asyncio
@respx.mock
async def test_snapshot_falls_back_to_raw_close_when_adjusted_missing() -> None:
    end = date(2026, 1, 30)
    days = _consecutive_dates(end=end, count=5)
    bars: list[dict[str, object]] = []
    for d in days:
        bar = _bar(day=d, code="13010", close="100.0")
        bar["AdjC"] = None
        bars.append(bar)
    respx.get(f"{_BASE}/equities/bars/daily").mock(
        return_value=httpx.Response(200, json=_bars_payload(bars)),
    )

    async with JQuantsClient(api_key=_KEY) as client:
        snapshot = await fetch_jquants_asset_snapshot(client=client, code="13010")

    assert snapshot.last_close == Decimal("100.0")


@pytest.mark.asyncio
@respx.mock
async def test_snapshot_passes_code_and_window_to_jquants() -> None:
    """as_of -> from = as_of - 365 days, to = as_of, code carried through."""
    as_of = date(2026, 1, 30)
    days = _consecutive_dates(end=as_of, count=5)
    bars = [_bar(day=d, code="13010", close="100.0") for d in days]
    route = respx.get(f"{_BASE}/equities/bars/daily").mock(
        return_value=httpx.Response(200, json=_bars_payload(bars)),
    )

    async with JQuantsClient(api_key=_KEY) as client:
        await fetch_jquants_asset_snapshot(client=client, code="13010", as_of=as_of)

    params = route.calls.last.request.url.params
    assert params["code"] == "13010"
    assert params["from"] == "20250130"
    assert params["to"] == "20260130"


def test_consecutive_dates_helper() -> None:
    end = date(2026, 1, 30)
    out = _consecutive_dates(end=end, count=3)
    assert out == [date(2026, 1, 28), date(2026, 1, 29), date(2026, 1, 30)]


@pytest.mark.asyncio
@respx.mock
async def test_snapshot_without_as_of_omits_date_params() -> None:
    """When as_of is None the helper must not pass from/to to J-Quants.

    The free-tier subscription window has a defined start and end;
    querying past the end with to=today returns HTTP 400. Calling
    daily_bars with only `code` lets the API return its full
    available history without risking a window-mismatch error.
    """
    end = date(2026, 1, 30)
    days = _consecutive_dates(end=end, count=5)
    bars = [_bar(day=d, code="13010", close="100.0") for d in days]
    route = respx.get(f"{_BASE}/equities/bars/daily").mock(
        return_value=httpx.Response(200, json=_bars_payload(bars)),
    )

    async with JQuantsClient(api_key=_KEY) as client:
        await fetch_jquants_asset_snapshot(client=client, code="13010")

    params = route.calls.last.request.url.params
    assert "from" not in params
    assert "to" not in params
    assert params["code"] == "13010"
