"""Live smoke test for PolymarketArchiveClient.

Gated by ``CAQRS_LIVE=1``. Downloads one recent hourly parquet from
archive.pmxt.dev (~100-400 MB) and confirms:

- the file is fetched into the cache without raising
- the parquet parses with polars
- the documented columns (``market``, ``event_type``, ``asset_id``,
  ``timestamp``, ``timestamp_received``) are present
- ``event_type`` values fall inside the documented enum set

No LLM endpoint or API key needed; the archive is public HTTPS.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from caqrs.data.polymarket_archive import (
    ArchiveEventKind,
    PolymarketArchiveClient,
    load_events,
)


def _recent_hour() -> datetime:
    """Pick the most-recently completed hour boundary in UTC.

    The archive publishes after each hour completes, so 'now floored
    to the hour' might race against publication. Step back two hours
    to be safely past the publication window.
    """
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    return now - timedelta(hours=2)


@pytest.mark.live
@pytest.mark.asyncio
async def test_archive_fetch_and_parse(tmp_path: Path) -> None:
    hour = _recent_hour()
    async with PolymarketArchiveClient(cache_dir=tmp_path) as client:
        path = await client.fetch_hour(hour)

    assert path.exists()
    expected_min_size = 1_000  # any meaningful parquet is at least this big
    assert path.stat().st_size > expected_min_size

    df = pl.read_parquet(path)
    assert df.height > 0

    expected_columns = {
        "market",
        "event_type",
        "asset_id",
        "timestamp",
        "timestamp_received",
    }
    missing = expected_columns - set(df.columns)
    assert not missing, f"archive parquet missing columns: {missing}"

    distinct_kinds = set(df["event_type"].unique().to_list())
    documented = {kind.value for kind in ArchiveEventKind}
    unexpected = distinct_kinds - documented
    assert not unexpected, f"archive parquet has undocumented event_type values: {unexpected}"


@pytest.mark.live
@pytest.mark.asyncio
async def test_archive_load_events_filter(tmp_path: Path) -> None:
    """load_events filter helpers compose with a real parquet."""
    hour = _recent_hour()
    async with PolymarketArchiveClient(cache_dir=tmp_path) as client:
        path = await client.fetch_hour(hour)

    book_only = load_events(paths=[path], event_type=ArchiveEventKind.BOOK)
    all_kinds = load_events(paths=[path])
    # If both are non-empty the filter actually narrowed the data
    if all_kinds.height > 0 and book_only.height > 0:
        assert book_only.height <= all_kinds.height
        assert set(book_only["event_type"].unique().to_list()) == {"book"}
