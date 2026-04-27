"""Tests for the load_events polars filter helper."""

from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from caqrs.data.polymarket_archive import (
    ArchiveEventKind,
    load_events,
)

_MARKET_A = b"0x" + b"a" * 64
_MARKET_B = b"0x" + b"b" * 64


def _write_fixture(path: Path) -> None:
    df = pl.DataFrame(
        {
            "timestamp_received": [
                datetime(2026, 4, 27, 8, 0, 0, tzinfo=UTC),
                datetime(2026, 4, 27, 8, 0, 1, tzinfo=UTC),
                datetime(2026, 4, 27, 8, 0, 2, tzinfo=UTC),
                datetime(2026, 4, 27, 8, 0, 3, tzinfo=UTC),
            ],
            "timestamp": [
                datetime(2026, 4, 27, 8, 0, 0, tzinfo=UTC),
                datetime(2026, 4, 27, 8, 0, 1, tzinfo=UTC),
                datetime(2026, 4, 27, 8, 0, 2, tzinfo=UTC),
                datetime(2026, 4, 27, 8, 0, 3, tzinfo=UTC),
            ],
            "market": [_MARKET_A, _MARKET_A, _MARKET_B, _MARKET_A],
            "event_type": ["book", "price_change", "book", "last_trade_price"],
            "asset_id": ["100", "100", "200", "100"],
        },
    )
    df.write_parquet(path)


def test_archive_event_kind_enum_covers_documented_values() -> None:
    """The four event_type values listed in the v2 schema doc."""
    values = {kind.value for kind in ArchiveEventKind}
    assert values == {"book", "price_change", "last_trade_price", "tick_size_change"}


def test_load_events_no_filter_returns_all_rows(tmp_path: Path) -> None:
    f = tmp_path / "events.parquet"
    _write_fixture(f)
    df = load_events(paths=[f])
    expected_rows = 4
    assert df.height == expected_rows


def test_load_events_filters_by_market(tmp_path: Path) -> None:
    f = tmp_path / "events.parquet"
    _write_fixture(f)
    df = load_events(paths=[f], market="0x" + "a" * 64)
    expected_rows = 3  # 3 rows have market_a
    assert df.height == expected_rows


def test_load_events_filters_by_asset_id(tmp_path: Path) -> None:
    f = tmp_path / "events.parquet"
    _write_fixture(f)
    df = load_events(paths=[f], asset_id="200")
    assert df.height == 1
    assert df["event_type"][0] == "book"


def test_load_events_filters_by_event_type_via_enum(tmp_path: Path) -> None:
    f = tmp_path / "events.parquet"
    _write_fixture(f)
    df = load_events(paths=[f], event_type=ArchiveEventKind.BOOK)
    expected_rows = 2  # 2 book events
    assert df.height == expected_rows


def test_load_events_filters_combine(tmp_path: Path) -> None:
    f = tmp_path / "events.parquet"
    _write_fixture(f)
    df = load_events(
        paths=[f],
        market="0x" + "a" * 64,
        asset_id="100",
        event_type=ArchiveEventKind.PRICE_CHANGE,
    )
    assert df.height == 1


def test_load_events_returns_empty_when_no_match(tmp_path: Path) -> None:
    f = tmp_path / "events.parquet"
    _write_fixture(f)
    df = load_events(paths=[f], asset_id="999")
    assert df.height == 0


def test_load_events_across_multiple_paths(tmp_path: Path) -> None:
    f1 = tmp_path / "h1.parquet"
    f2 = tmp_path / "h2.parquet"
    _write_fixture(f1)
    _write_fixture(f2)
    df = load_events(paths=[f1, f2])
    expected_rows = 8  # 4 rows per fixture x 2 files
    assert df.height == expected_rows


def test_load_events_rejects_empty_path_list(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one"):
        load_events(paths=[])
