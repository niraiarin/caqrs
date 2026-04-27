"""Polymarket historical archive (parquet snapshots).

Polymarket's public CLOB API serves point-in-time orderbook state but
no usable historical depth — ``/prices-history`` covers only one
token's midpoint at a single interval. For backtesting / regret
analysis the archive at https://archive.pmxt.dev/Polymarket/ exposes
hourly Parquet snapshots of every Polymarket WebSocket market-channel
event (book / price_change / last_trade_price / tick_size_change).
This module wraps that archive.

Requires the ``archive`` extras::

    pip install caqrs[archive]
    # or, with uv:
    uv add 'caqrs[archive]'

The dependency (`polars`) is heavy; only callers that actually need
historical Polymarket data should pull it in.
"""

from caqrs.data.polymarket.clob_client import PolymarketError
from caqrs.data.polymarket_archive.client import PolymarketArchiveClient
from caqrs.data.polymarket_archive.query import ArchiveEventKind, load_events

__all__ = [
    "ArchiveEventKind",
    "PolymarketArchiveClient",
    "PolymarketError",
    "load_events",
]
