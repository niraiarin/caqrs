"""TTL SQLite cache for EDINET DB responses.

Required for any production use given the free-plan **100 req/day**
quota. Mirrors :mod:`caqrs.data.yfinance.cache`: SQLite-backed,
JSON-serialised payloads, per-entry ``expires_at`` so different
endpoint families can have different TTLs in the same store.

Default TTLs reflect upstream change cadence:

- **Companies master** (7 days). New listings happen daily but the
  full master is small and stable across consecutive trading days.
- **Financials** (30 days). Driven by quarterly filings; mid-quarter
  fiscal-year rows don't change.
- **Rankings** (7 days). Recomputed at fiscal-year boundaries.

The cache is **opt-in** via :class:`EdinetDbClient(cache=...)`.
Callers running unattended supervisors should always supply one;
ad-hoc REPL calls can omit it.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from caqrs.data.edinetdb.schemas import (
    EdinetDbCompaniesList,
    EdinetDbFinancialPeriod,
    EdinetDbRoeRanking,
)

# TTL constants per endpoint cadence.
DEFAULT_COMPANIES_TTL_SECONDS: Final[int] = 7 * 86_400
DEFAULT_FINANCIALS_TTL_SECONDS: Final[int] = 30 * 86_400
DEFAULT_RANKINGS_TTL_SECONDS: Final[int] = 7 * 86_400


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS entries (
    cache_key TEXT PRIMARY KEY,
    expires_at REAL NOT NULL,
    payload TEXT NOT NULL
)
"""


class EdinetDbCache:
    """SQLite-backed TTL cache for EDINET DB endpoint responses.

    Construct once and share across :class:`EdinetDbClient` instances;
    the underlying file is the source of truth. Each entry has its
    own ``expires_at`` so the three endpoint families can co-exist
    with different TTLs in the same store.
    """

    def __init__(self, *, db_path: Path) -> None:
        self._db_path = Path(db_path)

    # === Companies ===

    def get_companies(
        self,
        *,
        page: int,
        per_page: int,
    ) -> EdinetDbCompaniesList | None:
        key = f"companies:page={page}:per_page={per_page}"
        row = self._read_row(key)
        if row is None:
            return None
        expires_at, payload = row
        if expires_at <= time.time():
            return None
        return EdinetDbCompaniesList.model_validate_json(payload)

    def set_companies(
        self,
        *,
        page: int,
        per_page: int,
        listing: EdinetDbCompaniesList,
        ttl_seconds: int,
    ) -> None:
        key = f"companies:page={page}:per_page={per_page}"
        self._write_row(
            key=key,
            expires_at=time.time() + ttl_seconds,
            payload=listing.model_dump_json(),
        )

    # === Financials ===

    def get_financials(
        self,
        *,
        edinet_code: str,
    ) -> tuple[EdinetDbFinancialPeriod, ...] | None:
        key = f"financials:{edinet_code}"
        row = self._read_row(key)
        if row is None:
            return None
        expires_at, payload = row
        if expires_at <= time.time():
            return None
        records = json.loads(payload)
        return tuple(EdinetDbFinancialPeriod.model_validate(r) for r in records)

    def set_financials(
        self,
        *,
        edinet_code: str,
        rows: Sequence[EdinetDbFinancialPeriod],
        ttl_seconds: int,
    ) -> None:
        key = f"financials:{edinet_code}"
        payload = json.dumps([r.model_dump(mode="json") for r in rows])
        self._write_row(
            key=key,
            expires_at=time.time() + ttl_seconds,
            payload=payload,
        )

    # === Rankings ===

    def get_rankings(
        self,
        *,
        endpoint: str,
        limit: int,
    ) -> tuple[EdinetDbRoeRanking, ...] | None:
        """Return cached rows for the given ranking endpoint.

        ``endpoint`` is a short discriminator string (``"roe"``,
        future ``"per_share_growth"`` etc.). The schema is shared
        across ranking families per the EDINET DB v1 contract.
        """
        key = f"rankings:{endpoint}:limit={limit}"
        row = self._read_row(key)
        if row is None:
            return None
        expires_at, payload = row
        if expires_at <= time.time():
            return None
        records = json.loads(payload)
        return tuple(EdinetDbRoeRanking.model_validate(r) for r in records)

    def set_rankings(
        self,
        *,
        endpoint: str,
        limit: int,
        rows: Sequence[EdinetDbRoeRanking],
        ttl_seconds: int,
    ) -> None:
        key = f"rankings:{endpoint}:limit={limit}"
        payload = json.dumps([r.model_dump(mode="json") for r in rows])
        self._write_row(
            key=key,
            expires_at=time.time() + ttl_seconds,
            payload=payload,
        )

    # === Maintenance ===

    def clear(self) -> None:
        """Drop every entry — testing helper, also useful when the
        upstream API contract evolves and existing cached payloads
        no longer match the current schemas."""
        if not self._db_path.exists():
            return
        with self._connect() as conn:
            conn.execute("DELETE FROM entries")
            conn.commit()

    # === Internal ===

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # ``check_same_thread=False`` mirrors yfinance.cache — async
        # callers may invoke from worker threads via asyncio.to_thread
        # in future, and the connection is short-lived per write.
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.execute(_SCHEMA_SQL)
        return conn

    def _read_row(self, key: str) -> tuple[float, str] | None:
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT expires_at, payload FROM entries WHERE cache_key = ?",
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
                "INSERT OR REPLACE INTO entries (cache_key, expires_at, payload) VALUES (?, ?, ?)",
                (key, expires_at, payload),
            )
            conn.commit()
