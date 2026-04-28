"""Persistent daily-quota tracker for EDINET DB requests.

The cache shipped in the previous slice eliminates *repeated*
requests; this module enforces a hard ceiling on the *absolute*
count per UTC day. SQLite-backed so the count survives process
restarts (a CycleRunner restart 5 minutes before midnight should
not reset the day's budget).

Architectural decision: the tracker counts only **upstream HTTP
calls**, not cache hits. Hits never touch the API and don't consume
quota. The :class:`EdinetDbClient` calls
:meth:`DailyQuotaTracker.record_request` *after* a successful HTTP
fetch, and :meth:`assert_quota_available` *before* every fetch
attempt.

Day boundary is **UTC**, matching the most common cron / supervisor
schedule. Callers in non-UTC timezones can supply their own ``now``
to either method to align with their local trading day if needed.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

_DEFAULT_DAILY_CAP = 100  # EDINET DB free-plan budget

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS quota_log (
    timestamp REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS quota_log_ts_idx ON quota_log(timestamp);
"""


class EdinetDbQuotaExhaustedError(Exception):
    """Raised when an attempted HTTP call would exceed the daily cap.

    Carries the current cap and today's count so callers can format
    a human-readable retry-at message without re-querying the
    tracker.
    """

    def __init__(self, *, cap: int, used: int) -> None:
        super().__init__(
            f"EDINET DB daily quota exhausted: {used}/{cap} requests "
            "used today (UTC). Wait until 00:00 UTC for the budget to reset."
        )
        self.cap = cap
        self.used = used


class DailyQuotaTracker:
    """SQLite-backed counter of EDINET DB requests per UTC day.

    Construct once, share across :class:`EdinetDbClient` instances
    if you have multiple clients hitting the same upstream account.
    The underlying file is the source of truth.
    """

    def __init__(
        self,
        *,
        db_path: Path,
        daily_cap: int = _DEFAULT_DAILY_CAP,
    ) -> None:
        if daily_cap <= 0:
            msg = f"daily_cap must be positive; got {daily_cap}"
            raise ValueError(msg)
        self._db_path = Path(db_path)
        self._daily_cap = daily_cap

    @property
    def daily_cap(self) -> int:
        return self._daily_cap

    # === Public API ===

    def record_request(self, *, now: datetime | None = None) -> None:
        """Append a row representing one upstream HTTP call.

        Default ``now`` is :func:`datetime.now(UTC)`; pass an explicit
        timezone-aware value when integrating with deterministic test
        clocks.
        """
        ts = (now or datetime.now(UTC)).timestamp()
        with self._connect() as conn:
            conn.execute("INSERT INTO quota_log (timestamp) VALUES (?)", (ts,))
            conn.commit()

    def requests_today(self, *, now: datetime | None = None) -> int:
        """Number of recorded requests within ``now``'s UTC calendar
        day — ``[00:00, next 00:00)``. Bounded both ends so passing a
        past or future ``now`` correctly isolates that single day."""
        when = now or datetime.now(UTC)
        floor = self._utc_day_floor(when)
        ceiling = floor + 86_400.0
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM quota_log WHERE timestamp >= ? AND timestamp < ?",
                (floor, ceiling),
            )
            row = cursor.fetchone()
        return int(row[0]) if row else 0

    def quota_remaining(self, *, now: datetime | None = None) -> int:
        """``daily_cap`` minus today's count, clamped at 0."""
        return max(0, self._daily_cap - self.requests_today(now=now))

    def assert_quota_available(self, *, now: datetime | None = None) -> None:
        """Raise :class:`EdinetDbQuotaExhaustedError` if today's
        count has reached the cap."""
        used = self.requests_today(now=now)
        if used >= self._daily_cap:
            raise EdinetDbQuotaExhaustedError(cap=self._daily_cap, used=used)

    def purge_older_than(self, *, cutoff: datetime) -> int:
        """Delete rows older than ``cutoff``; return number deleted.

        Long-lived deployments accumulate timestamps over months;
        callers should run this once a week or so against a
        ~30-day-old cutoff to keep the file small.
        """
        ts = cutoff.timestamp()
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM quota_log WHERE timestamp < ?", (ts,))
            conn.commit()
            return int(cursor.rowcount or 0)

    # === Internal ===

    @staticmethod
    def _utc_day_floor(when: datetime) -> float:
        """Return the unix timestamp for 00:00:00 UTC on ``when``'s
        calendar day."""
        floor = when.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return floor.timestamp()

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.executescript(_SCHEMA_SQL)
        return conn
