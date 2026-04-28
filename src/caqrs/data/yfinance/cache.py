"""TTL SQLite cache for yFinance responses.

Implements the "prefetch then replay" tactic from the Zenn rakuscan-
data-layer-pipeline article: aggressive caching shields the unofficial
yfinance scraping endpoint from rate-limit bans, and the article
reports a 99% reduction in upstream calls under steady-state usage.

The cache is **opt-in** — :class:`YFinanceClient` accepts an optional
``cache`` constructor arg, and without it every call hits Yahoo. The
SQLite file is stable and survives process restart, so a long-running
supervisor (or a screening pipeline that prefetches once and replays
many times) builds up coverage as it runs.

Default TTLs come straight from the article:

- Prices: 1 day (close-of-business is invariant intraday).
- Financials: 30 days (10-Q / 10-K filings are quarterly events).
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Sequence
from datetime import date as _date
from pathlib import Path
from typing import Final

from caqrs.data.yfinance.schemas import YFinancePrice

# TTL constants per Zenn rakuscan article.
DEFAULT_PRICES_TTL_SECONDS: Final[int] = 86_400  # 1 day
DEFAULT_FINANCIALS_TTL_SECONDS: Final[int] = 30 * 86_400  # 30 days


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS bars (
    cache_key TEXT PRIMARY KEY,
    expires_at REAL NOT NULL,
    payload TEXT NOT NULL
)
"""


class YFinanceCache:
    """SQLite-backed TTL cache for yfinance bar responses.

    Construct once and share across :class:`YFinanceClient` instances;
    the underlying SQLite file is the source of truth. Each entry has
    its own ``expires_at`` so prices and financials can co-exist with
    different TTLs.
    """

    def __init__(self, *, db_path: Path) -> None:
        self._db_path = Path(db_path)

    # === Bars ===

    def get_bars(
        self,
        *,
        symbol: str,
        from_date: _date,
        to_date: _date,
    ) -> tuple[YFinancePrice, ...] | None:
        """Return cached bars for the exact ``(symbol, from, to)`` key.

        Returns ``None`` for cache miss **or** expired entry; callers
        treat both equivalently. Misses are silent (no logging) so
        cycle runs aren't noisy.
        """
        key = self._bar_key(symbol=symbol, from_date=from_date, to_date=to_date)
        row = self._read_row(key)
        if row is None:
            return None
        expires_at, payload = row
        if expires_at <= time.time():
            return None
        records = json.loads(payload)
        # JSON-mode coercion (string→date, string→Decimal) is the
        # canonical shape for re-hydrating from on-disk storage; the
        # cache is a serialisation boundary, not a CAQRS-internal hop.
        return tuple(YFinancePrice.model_validate(r, strict=False) for r in records)

    def set_bars(
        self,
        *,
        symbol: str,
        from_date: _date,
        to_date: _date,
        bars: Sequence[YFinancePrice],
        ttl_seconds: int,
    ) -> None:
        """Cache the bars for ``ttl_seconds`` from now.

        ``ttl_seconds=0`` means immediate expiry — useful for tests
        and for a "no-cache" override path.
        """
        key = self._bar_key(symbol=symbol, from_date=from_date, to_date=to_date)
        # mode="json" so dates / Decimals serialise as ISO strings /
        # numeric strings without bespoke encoders.
        payload = json.dumps([bar.model_dump(mode="json") for bar in bars])
        expires_at = time.time() + ttl_seconds
        self._write_row(key=key, expires_at=expires_at, payload=payload)

    def clear(self) -> None:
        """Drop every entry (testing helper)."""
        if not self._db_path.exists():
            return
        with self._connect() as conn:
            conn.execute("DELETE FROM bars")
            conn.commit()

    # === Internal ===

    @staticmethod
    def _bar_key(*, symbol: str, from_date: _date, to_date: _date) -> str:
        return f"bars:{symbol}:{from_date.isoformat()}:{to_date.isoformat()}"

    def _connect(self) -> sqlite3.Connection:
        # Lazy directory creation so callers don't have to mkdir.
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # ``check_same_thread=False`` is safe here because all writes
        # are short-lived and we don't share the connection across
        # threads. asyncio.to_thread spawns a fresh worker thread per
        # call, which would otherwise trip SQLite's thread guard.
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.execute(_SCHEMA_SQL)
        return conn

    def _read_row(self, key: str) -> tuple[float, str] | None:
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT expires_at, payload FROM bars WHERE cache_key = ?",
                (key,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        expires_at, payload = row
        return float(expires_at), str(payload)

    def _write_row(self, *, key: str, expires_at: float, payload: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO bars (cache_key, expires_at, payload) VALUES (?, ?, ?)",
                (key, expires_at, payload),
            )
            conn.commit()
