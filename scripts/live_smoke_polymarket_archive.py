#!/usr/bin/env python3
"""Standalone smoke check for the Polymarket archive adapter.

Downloads one recent hourly Parquet snapshot from archive.pmxt.dev,
parses it with polars, and prints summary stats: row count, distinct
markets, distinct asset_ids, event_type counts. Useful for verifying
the schema assumptions before committing or after an archive
publication change.

No API key; the archive is public HTTPS. Each file is 100-400 MB so
the first run downloads for ~10-30 s, subsequent runs read from
the on-disk cache.

Usage::

    uv run python scripts/live_smoke_polymarket_archive.py
    uv run python scripts/live_smoke_polymarket_archive.py --hour 2026-04-27T08
    uv run python scripts/live_smoke_polymarket_archive.py --cache /tmp/pmxt-cache
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl

from caqrs.data.polymarket_archive import (
    ArchiveEventKind,
    PolymarketArchiveClient,
    PolymarketError,
)


def _parse_hour(value: str) -> datetime:
    """Parse YYYY-MM-DDTHH (UTC) into a tz-aware datetime."""
    return datetime.strptime(value, "%Y-%m-%dT%H").replace(tzinfo=UTC)


def _default_hour() -> datetime:
    """Most recent fully-published hour (publication ~1h after the boundary)."""
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    return now - timedelta(hours=2)


async def _run(hour: datetime, cache: Path) -> int:
    print(f"[fetch] hour={hour.isoformat()}  cache={cache}")
    try:
        async with PolymarketArchiveClient(cache_dir=cache) as client:
            path = await client.fetch_hour(hour)
    except PolymarketError as exc:
        status = exc.status_code if exc.status_code is not None else "(no status)"
        print(f"[error] PolymarketError ({status}): {exc}", file=sys.stderr)
        return 1

    size_mb = path.stat().st_size / (1024 * 1024)
    print(f"[fetch] cached at {path} ({size_mb:.1f} MB)")

    df = pl.read_parquet(path)
    print(f"[parse] rows={df.height:,}  cols={len(df.columns)}")
    print(f"[parse] columns: {df.columns}")

    distinct_markets = df.select(pl.col("market").n_unique()).item()
    distinct_assets = df.select(pl.col("asset_id").n_unique()).item()
    print(f"[summary] distinct markets : {distinct_markets:,}")
    print(f"[summary] distinct asset_ids: {distinct_assets:,}")

    by_kind = df.group_by("event_type").agg(pl.len().alias("count")).sort("count", descending=True)
    print("[summary] event_type counts:")
    for row in by_kind.iter_rows(named=True):
        marker = "  " if row["event_type"] in {k.value for k in ArchiveEventKind} else "?!"
        print(f"  {marker}{row['event_type']:<22} {row['count']:>10,}")
    undocumented = set(by_kind["event_type"].to_list()) - {k.value for k in ArchiveEventKind}
    if undocumented:
        print(
            f"[warn] event_type contains undocumented values: {undocumented}",
            file=sys.stderr,
        )
        return 1
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--hour",
        type=_parse_hour,
        default=None,
        help="UTC hour to fetch in YYYY-MM-DDTHH form (default: 2 hours ago).",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=None,
        help="Cache directory (default: a per-invocation temp dir).",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    hour = args.hour if args.hour is not None else _default_hour()
    cache = args.cache if args.cache is not None else Path(tempfile.mkdtemp(prefix="pmxt-"))
    return asyncio.run(_run(hour, cache))


if __name__ == "__main__":
    sys.exit(main())
