"""Tests for PolymarketArchiveClient — hourly parquet fetch + cache."""

import io
from datetime import UTC, datetime
from pathlib import Path

import httpx
import polars as pl
import pytest
import respx

from caqrs.data.polymarket_archive import (
    PolymarketArchiveClient,
    PolymarketError,
)

_BASE = "https://r2v2.pmxt.dev"


def _parquet_bytes() -> bytes:
    """Synthesize a tiny parquet payload that matches the live schema
    well enough for transport tests."""
    df = pl.DataFrame(
        {
            "timestamp_received": [datetime(2026, 4, 27, 8, 0, 0, tzinfo=UTC)],
            "timestamp": [datetime(2026, 4, 27, 8, 0, 0, tzinfo=UTC)],
            "market": [b"0x" + b"a" * 64],
            "event_type": ["book"],
            "asset_id": ["100"],
        },
    )
    buf = io.BytesIO()
    df.write_parquet(buf)
    return buf.getvalue()


# === URL / cache layout ===


def test_cached_path_uses_hour_naming_convention(tmp_path: Path) -> None:
    client = PolymarketArchiveClient(cache_dir=tmp_path)
    hour = datetime(2026, 4, 27, 8, 0, 0, tzinfo=UTC)
    expected = tmp_path / "polymarket_orderbook_2026-04-27T08.parquet"
    assert client.cached_path(hour) == expected


def test_cached_path_truncates_minute_and_second(tmp_path: Path) -> None:
    client = PolymarketArchiveClient(cache_dir=tmp_path)
    hour = datetime(2026, 4, 27, 8, 17, 42, tzinfo=UTC)
    expected = tmp_path / "polymarket_orderbook_2026-04-27T08.parquet"
    assert client.cached_path(hour) == expected


def test_cached_path_requires_tz_aware_datetime(tmp_path: Path) -> None:
    client = PolymarketArchiveClient(cache_dir=tmp_path)
    naive = datetime(2026, 4, 27, 8, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        client.cached_path(naive)


# === fetch_hour ===


@pytest.mark.asyncio
@respx.mock
async def test_fetch_hour_downloads_when_cache_miss(tmp_path: Path) -> None:
    payload = _parquet_bytes()
    route = respx.get(f"{_BASE}/polymarket_orderbook_2026-04-27T08.parquet").mock(
        return_value=httpx.Response(200, content=payload),
    )
    client = PolymarketArchiveClient(cache_dir=tmp_path)
    path = await client.fetch_hour(datetime(2026, 4, 27, 8, tzinfo=UTC))
    assert path.exists()
    assert path.read_bytes() == payload
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_fetch_hour_uses_cache_on_second_call(tmp_path: Path) -> None:
    payload = _parquet_bytes()
    route = respx.get(f"{_BASE}/polymarket_orderbook_2026-04-27T08.parquet").mock(
        return_value=httpx.Response(200, content=payload),
    )
    client = PolymarketArchiveClient(cache_dir=tmp_path)
    hour = datetime(2026, 4, 27, 8, tzinfo=UTC)
    await client.fetch_hour(hour)
    await client.fetch_hour(hour)
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_fetch_hour_writes_atomically(tmp_path: Path) -> None:
    """Crash mid-download must leave no half-written file in the cache."""
    respx.get(f"{_BASE}/polymarket_orderbook_2026-04-27T08.parquet").mock(
        side_effect=httpx.ReadError("boom"),
    )
    client = PolymarketArchiveClient(cache_dir=tmp_path)
    hour = datetime(2026, 4, 27, 8, tzinfo=UTC)
    with pytest.raises(PolymarketError):
        await client.fetch_hour(hour)
    assert not (tmp_path / "polymarket_orderbook_2026-04-27T08.parquet").exists()
    # The .tmp must also be cleaned up
    assert not (tmp_path / "polymarket_orderbook_2026-04-27T08.parquet.tmp").exists()


@pytest.mark.asyncio
@respx.mock
async def test_fetch_hour_404_raises_with_status_code(tmp_path: Path) -> None:
    respx.get(f"{_BASE}/polymarket_orderbook_2026-04-27T08.parquet").mock(
        return_value=httpx.Response(404, text="not found"),
    )
    client = PolymarketArchiveClient(cache_dir=tmp_path)
    with pytest.raises(PolymarketError) as exc_info:
        await client.fetch_hour(datetime(2026, 4, 27, 8, tzinfo=UTC))
    assert exc_info.value.status_code == 404


# === fetch_range ===


@pytest.mark.asyncio
@respx.mock
async def test_fetch_range_returns_paths_in_chronological_order(tmp_path: Path) -> None:
    payload = _parquet_bytes()
    for h in (8, 9, 10):
        respx.get(f"{_BASE}/polymarket_orderbook_2026-04-27T{h:02d}.parquet").mock(
            return_value=httpx.Response(200, content=payload),
        )
    client = PolymarketArchiveClient(cache_dir=tmp_path)
    paths = await client.fetch_range(
        start=datetime(2026, 4, 27, 8, tzinfo=UTC),
        end=datetime(2026, 4, 27, 11, tzinfo=UTC),  # exclusive
    )
    assert [p.name for p in paths] == [
        "polymarket_orderbook_2026-04-27T08.parquet",
        "polymarket_orderbook_2026-04-27T09.parquet",
        "polymarket_orderbook_2026-04-27T10.parquet",
    ]


@pytest.mark.asyncio
@respx.mock
async def test_fetch_range_end_is_exclusive(tmp_path: Path) -> None:
    """end is treated as exclusive — an end of 09:00 returns only the 08 hour."""
    respx.get(f"{_BASE}/polymarket_orderbook_2026-04-27T08.parquet").mock(
        return_value=httpx.Response(200, content=_parquet_bytes()),
    )
    client = PolymarketArchiveClient(cache_dir=tmp_path)
    paths = await client.fetch_range(
        start=datetime(2026, 4, 27, 8, tzinfo=UTC),
        end=datetime(2026, 4, 27, 9, tzinfo=UTC),
    )
    assert len(paths) == 1


@pytest.mark.asyncio
async def test_fetch_range_rejects_end_before_start(tmp_path: Path) -> None:
    client = PolymarketArchiveClient(cache_dir=tmp_path)
    with pytest.raises(ValueError, match="end must be"):
        await client.fetch_range(
            start=datetime(2026, 4, 27, 9, tzinfo=UTC),
            end=datetime(2026, 4, 27, 8, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_fetch_range_rejects_naive_datetimes(tmp_path: Path) -> None:
    client = PolymarketArchiveClient(cache_dir=tmp_path)
    with pytest.raises(ValueError, match="timezone-aware"):
        await client.fetch_range(
            start=datetime(2026, 4, 27, 8),
            end=datetime(2026, 4, 27, 9, tzinfo=UTC),
        )
