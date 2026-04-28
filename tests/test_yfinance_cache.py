"""TTL SQLite cache for yFinance responses.

Mirrors the rakuscan-data-layer-pipeline article's "99% API call
reduction" tactic: aggressive prefetch into a TTL'd SQLite, treat
external APIs as a fallback. Default TTLs come straight from the
article: 1 day for prices, 30 days for financials.

The cache is **opt-in**: callers construct a :class:`YFinanceCache`
and pass it to :class:`YFinanceClient`. Without one, the client
behaves exactly as the P1 yfinance ship — every call hits Yahoo.
"""

from __future__ import annotations

import time
from datetime import date
from decimal import Decimal
from pathlib import Path

from caqrs.data.yfinance.cache import (
    DEFAULT_FINANCIALS_TTL_SECONDS,
    DEFAULT_PRICES_TTL_SECONDS,
    YFinanceCache,
)
from caqrs.data.yfinance.schemas import YFinancePrice


def _bar(symbol: str, day: date, close: Decimal) -> YFinancePrice:
    return YFinancePrice(
        symbol=symbol,
        date=day,
        open=close,
        high=close + Decimal("1"),
        low=close - Decimal("1"),
        close=close,
        adjusted_close=None,
        volume=1_000_000,
    )


# === Construction ===


def test_cache_creates_db_file_lazily(tmp_path: Path) -> None:
    db = tmp_path / "yf-cache.db"
    cache = YFinanceCache(db_path=db)
    # File appears on first write, not at construction.
    assert not db.exists()
    cache.set_bars(
        symbol="AAPL",
        from_date=date(2026, 4, 1),
        to_date=date(2026, 4, 28),
        bars=(),
        ttl_seconds=86400,
    )
    assert db.exists()


# === Round-trip ===


def test_cache_round_trips_bars(tmp_path: Path) -> None:
    cache = YFinanceCache(db_path=tmp_path / "yf-cache.db")
    bars = (
        _bar("AAPL", date(2026, 4, 25), Decimal("180")),
        _bar("AAPL", date(2026, 4, 26), Decimal("181")),
    )
    cache.set_bars(
        symbol="AAPL",
        from_date=date(2026, 4, 25),
        to_date=date(2026, 4, 26),
        bars=bars,
        ttl_seconds=86400,
    )
    fetched = cache.get_bars(
        symbol="AAPL",
        from_date=date(2026, 4, 25),
        to_date=date(2026, 4, 26),
    )
    assert fetched == bars


def test_cache_miss_returns_none(tmp_path: Path) -> None:
    cache = YFinanceCache(db_path=tmp_path / "yf-cache.db")
    assert (
        cache.get_bars(
            symbol="UNKNOWN",
            from_date=date(2026, 4, 25),
            to_date=date(2026, 4, 26),
        )
        is None
    )


def test_different_date_range_misses_even_for_same_symbol(tmp_path: Path) -> None:
    """Cache key is (symbol, from_date, to_date); narrower / wider
    ranges do not hit. The client always asks for the exact range
    its caller wants."""
    cache = YFinanceCache(db_path=tmp_path / "yf-cache.db")
    cache.set_bars(
        symbol="AAPL",
        from_date=date(2026, 4, 25),
        to_date=date(2026, 4, 26),
        bars=(_bar("AAPL", date(2026, 4, 25), Decimal("180")),),
        ttl_seconds=86400,
    )
    # Different to_date.
    assert (
        cache.get_bars(
            symbol="AAPL",
            from_date=date(2026, 4, 25),
            to_date=date(2026, 4, 27),
        )
        is None
    )


# === TTL expiry ===


def test_expired_entry_returns_none(tmp_path: Path) -> None:
    """Ttl=0 means immediate expiry. Effectively a "no-cache" mode."""
    cache = YFinanceCache(db_path=tmp_path / "yf-cache.db")
    cache.set_bars(
        symbol="AAPL",
        from_date=date(2026, 4, 25),
        to_date=date(2026, 4, 26),
        bars=(_bar("AAPL", date(2026, 4, 25), Decimal("180")),),
        ttl_seconds=0,
    )
    # Allow even the smallest clock skew.
    time.sleep(0.01)
    assert (
        cache.get_bars(
            symbol="AAPL",
            from_date=date(2026, 4, 25),
            to_date=date(2026, 4, 26),
        )
        is None
    )


# === Persistence across instances ===


def test_cache_persists_across_instances(tmp_path: Path) -> None:
    """Two clients sharing the same db file see each other's writes
    — that's the whole point of the rakuscan article's "prefetch
    once, query many" pattern."""
    db = tmp_path / "yf-cache.db"
    bars = (_bar("AAPL", date(2026, 4, 25), Decimal("180")),)

    writer = YFinanceCache(db_path=db)
    writer.set_bars(
        symbol="AAPL",
        from_date=date(2026, 4, 25),
        to_date=date(2026, 4, 25),
        bars=bars,
        ttl_seconds=86400,
    )

    reader = YFinanceCache(db_path=db)
    fetched = reader.get_bars(
        symbol="AAPL",
        from_date=date(2026, 4, 25),
        to_date=date(2026, 4, 25),
    )
    assert fetched == bars


# === Default TTLs match the article ===


def test_default_price_ttl_is_one_day() -> None:
    assert DEFAULT_PRICES_TTL_SECONDS == 86_400  # 24h


def test_default_financials_ttl_is_30_days() -> None:
    assert DEFAULT_FINANCIALS_TTL_SECONDS == 30 * 86_400


# === Cache invalidation helper ===


def test_clear_removes_all_entries(tmp_path: Path) -> None:
    cache = YFinanceCache(db_path=tmp_path / "yf-cache.db")
    cache.set_bars(
        symbol="AAPL",
        from_date=date(2026, 4, 25),
        to_date=date(2026, 4, 26),
        bars=(_bar("AAPL", date(2026, 4, 25), Decimal("180")),),
        ttl_seconds=86400,
    )
    cache.clear()
    assert (
        cache.get_bars(
            symbol="AAPL",
            from_date=date(2026, 4, 25),
            to_date=date(2026, 4, 26),
        )
        is None
    )
