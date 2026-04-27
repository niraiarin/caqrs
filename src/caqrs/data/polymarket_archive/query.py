"""Polars-based filter helpers for Polymarket archive parquet files.

The hourly parquet schema (per the v2 docs) treats every row as one
WebSocket event:

- ``timestamp_received`` / ``timestamp`` — tz-aware ms timestamps.
- ``market`` — fixed-size 66-byte ASCII condition id (``0x`` + 64 hex).
- ``event_type`` — one of ``book`` / ``price_change`` / ``last_trade_price`` / ``tick_size_change``.
- ``asset_id`` — outcome token id as a decimal string.
- nullable per-event columns: ``bids`` / ``asks`` (JSON), ``price``,
  ``size``, ``side``, ``best_bid``, ``best_ask``, ``fee_rate_bps``,
  ``transaction_hash``, ``old_tick_size``, ``new_tick_size``.

This module exposes a single :func:`load_events` filter helper. The
return value is a ``polars.DataFrame`` so callers can compose
arbitrary downstream analysis (group_by, join with ticker metadata,
midpoint reconstruction, etc.) without us trying to anticipate every
shape upfront.
"""

from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path

import polars as pl


class ArchiveEventKind(StrEnum):
    """The four event types a row in the archive parquet can be."""

    BOOK = "book"
    PRICE_CHANGE = "price_change"
    LAST_TRADE_PRICE = "last_trade_price"
    TICK_SIZE_CHANGE = "tick_size_change"


def load_events(
    *,
    paths: Iterable[Path],
    market: str | None = None,
    asset_id: str | None = None,
    event_type: ArchiveEventKind | str | None = None,
) -> pl.DataFrame:
    """Load + filter events across one or more hourly parquet files.

    Parameters
    ----------
    paths:
        Parquet files to read. Polars ``scan_parquet`` accepts a list
        and pushes filter predicates into the parquet reader so only
        matching row groups are decoded.
    market:
        Filter by condition id, e.g. ``"0x1234..."`` (66 chars). The
        underlying column is fixed-size binary; we ASCII-encode the
        string before comparing.
    asset_id:
        Filter by outcome token id (decimal string).
    event_type:
        Filter by event kind. Pass either an :class:`ArchiveEventKind`
        or its string value.

    Returns
    -------
    polars.DataFrame
        The materialised filtered frame. Column ordering matches the
        underlying parquet schema; downstream callers should index
        by name, not position.
    """
    paths_tuple = tuple(paths)
    if not paths_tuple:
        msg = "load_events requires at least one parquet path"
        raise ValueError(msg)

    lazy = pl.scan_parquet([str(p) for p in paths_tuple])

    if market is not None:
        market_bytes = market.encode("ascii")
        lazy = lazy.filter(pl.col("market") == market_bytes)
    if asset_id is not None:
        lazy = lazy.filter(pl.col("asset_id") == asset_id)
    if event_type is not None:
        kind_value = event_type.value if isinstance(event_type, ArchiveEventKind) else event_type
        lazy = lazy.filter(pl.col("event_type") == kind_value)

    return lazy.collect()
