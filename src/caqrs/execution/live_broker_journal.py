"""Durable submission + fills journal for the live broker.

Per Codex audit 2026-05-10 (PR #99 majors 3 + 4), the in-memory
``EventLog`` + ``BROKER_LIVE_SUBMITTED``-tail resolver pattern is
fragile across process restart, log rotation, and long venue latency.
This module provides the durable persistence layer the audit
flagged as a prerequisite for live (non-paper) trading.

Design (matches CAQRS's existing SQLite-cache style — see
:mod:`caqrs.data.edinetdb.cache` for the precedent):

- One SQLite database file (operator picks the path).
- Three tables:

  - ``submissions`` (PK ``client_order_id``): records what the broker
    submitted to Alpaca, including the ``cycle_id`` /
    ``decision_run_id`` for cycle attribution and the venue's
    ``order_id`` for replay disambiguation.
  - ``fills`` (auto-incrementing rowid): one row per
    ``BROKER_LIVE_FILLED`` event. Idempotent on
    ``(client_order_id, fill_id)`` when ``fill_id`` is supplied —
    Alpaca's at-least-once webhook delivery means the same fill may
    arrive twice and the journal MUST collapse them.
  - ``cancellations`` (auto-incrementing rowid): one row per
    ``BROKER_LIVE_CANCELLED`` event with the venue's reason string.

- Public API:

  - :meth:`register_submission` — called by
    :class:`LiveBrokerAlpaca._submit_to_alpaca` after each accepted order.
  - :meth:`record_fill` — called by the trade-update stream's
    ``consume`` loop on FILL / PARTIAL_FILL events.
  - :meth:`record_cancel` — same loop on CANCELED / REJECTED events.
  - :meth:`attribution` — resolver-side lookup
    (``client_order_id`` → ``(cycle_id, decision_run_id) | None``).
  - :meth:`make_resolvers` — convenience that returns the two
    callables :func:`caqrs.execution.alpaca_stream.consume` expects.

The journal is **opt-in** so unit tests that don't exercise the
durable path can still construct a LiveBrokerAlpaca without a SQLite
file. Production deployments MUST pass one.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from decimal import Decimal
from pathlib import Path
from types import TracebackType

from caqrs.execution.alpaca_stream import CycleIdResolver

# Schema version. Bumped any time _SCHEMA_SQL changes incompatibly.
# A journal opened against a DB whose user_version is newer than this
# (forward-incompatible upgrade) refuses to open; older versions
# trigger the migration runner. Per Codex audit 2026-05-10 finding 2.
_SCHEMA_VERSION = 1

# How long a writer should wait on a busy lock before raising
# "database is locked" — protects against transient contention
# between the broker (submission inserts) and the stream consumer
# (fill / cancel inserts) running on different threads.
_BUSY_TIMEOUT_MS = 5000

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS submissions (
    client_order_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    decision_run_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty TEXT NOT NULL,
    submitted_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL,
    fill_id TEXT,
    qty TEXT NOT NULL,
    fill_price_usd TEXT NOT NULL,
    is_partial INTEGER NOT NULL,
    recorded_at REAL NOT NULL,
    UNIQUE(client_order_id, fill_id)
);
CREATE TABLE IF NOT EXISTS cancellations (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL,
    reason TEXT,
    recorded_at REAL NOT NULL
);
"""


class LiveBrokerJournal:
    """Durable SQLite-backed log of live-broker submissions, fills,
    and cancellations.

    Constructed against a filesystem path (``Path(":memory:")`` works
    for tests). Schema is created on first open. Callers wrap the
    journal in ``with`` so the underlying connection closes
    deterministically; long-running processes can keep the journal
    open for the lifetime of the broker.
    """

    def __init__(self, *, path: Path) -> None:
        """Open or create the journal at ``path``.

        Concurrency contract (Codex audit 2026-05-10 finding 1): the
        connection is opened with ``check_same_thread=False`` AND
        guarded by an internal :class:`threading.Lock`, so the
        broker's submission writes and the stream's fill/cancel
        writes can share one journal instance across threads. WAL
        journal mode lets concurrent readers proceed without
        blocking writers; ``busy_timeout`` provides a 5-second
        retry window before raising ``database is locked``.

        Schema versioning (Codex audit finding 2): the journal
        records its schema version in ``PRAGMA user_version``.
        Opening a database whose stored version is **higher** than
        :data:`_SCHEMA_VERSION` raises — the operator MUST upgrade
        CAQRS before that journal can be used safely. Older versions
        run the migration runner (currently a no-op since v1 is
        the first release).
        """
        self._path = path
        self._conn = sqlite3.connect(
            str(path),
            check_same_thread=False,  # mediated by self._lock instead
            timeout=_BUSY_TIMEOUT_MS / 1000,
        )
        self._lock = threading.Lock()
        # File-backed databases benefit from WAL for concurrent
        # readers; ":memory:" databases ignore the pragma silently.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        # Read existing schema version; create v1 schema on first
        # open, refuse to open future versions.
        cur = self._conn.execute("PRAGMA user_version")
        existing_version = int(cur.fetchone()[0])
        if existing_version > _SCHEMA_VERSION:
            self._conn.close()
            msg = (
                f"LiveBrokerJournal at {path} has schema version "
                f"{existing_version}, newer than this CAQRS build's "
                f"supported version {_SCHEMA_VERSION}. Upgrade CAQRS "
                "before opening this journal."
            )
            raise RuntimeError(msg)
        if existing_version == 0:
            # Fresh database — install v1 schema.
            self._conn.executescript(_SCHEMA_SQL)
            self._conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            self._conn.commit()
        # existing_version in (1, ..., _SCHEMA_VERSION): no migration
        # needed yet (only v1 exists today). When future versions
        # land, the migration runner branches off this point.

    def __enter__(self) -> LiveBrokerJournal:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    # --- write surface ----------------------------------------------------

    def register_submission(
        self,
        *,
        client_order_id: str,
        cycle_id: str,
        decision_run_id: str,
        order_id: str,
        idempotency_key: str,
        symbol: str,
        side: str,
        qty: Decimal,
    ) -> None:
        """Record one accepted Alpaca submission. Called by
        :class:`LiveBrokerAlpaca._submit_to_alpaca` immediately after
        ``submit_order`` returns successfully.

        Re-registering the same ``client_order_id`` is a programmer
        error (sha256 collisions are negligible) and raises
        :class:`sqlite3.IntegrityError` so the operator notices."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO submissions "
                "(client_order_id, cycle_id, decision_run_id, order_id, "
                "idempotency_key, symbol, side, qty, submitted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    client_order_id,
                    cycle_id,
                    decision_run_id,
                    order_id,
                    idempotency_key,
                    symbol,
                    side,
                    str(qty),
                    time.time(),
                ),
            )
            self._conn.commit()

    def record_fill(
        self,
        *,
        client_order_id: str,
        qty: Decimal,
        fill_price_usd: Decimal,
        is_partial: bool,
        fill_id: str | None = None,
    ) -> bool:
        """Append one fill record. Returns ``True`` if newly inserted,
        ``False`` if a duplicate (same ``client_order_id`` + ``fill_id``)
        was suppressed — Alpaca's at-least-once webhook delivery means
        the same fill may arrive twice; the journal MUST collapse them.

        When ``fill_id`` is ``None`` the row is **audit-only** —
        every call inserts because SQL's UNIQUE constraint treats
        ``NULL`` as not-equal-to-NULL, so duplicate webhook delivery
        WILL produce duplicate rows. Per Codex audit 2026-05-10
        finding 4, emission consumers MUST NOT rely on this method's
        ``bool`` return for dedup decisions when ``fill_id`` is None
        — fall back to ``(client_order_id, qty, fill_price_usd)``
        equality is **explicitly unsafe** because legitimate partial
        fills can share quantity and price. The next-slice wiring
        MUST parse Alpaca's ``execution_id`` and supply it as
        ``fill_id`` before relying on dedup.
        """
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO fills "
                    "(client_order_id, fill_id, qty, fill_price_usd, is_partial, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        client_order_id,
                        fill_id,
                        str(qty),
                        str(fill_price_usd),
                        1 if is_partial else 0,
                        time.time(),
                    ),
                )
            except sqlite3.IntegrityError:
                return False
            self._conn.commit()
            return True

    def record_cancel(
        self,
        *,
        client_order_id: str,
        reason: str | None,
    ) -> bool:
        """Append one cancellation record. Returns ``True`` if newly
        inserted, ``False`` if a cancellation for ``client_order_id``
        was already on file — Alpaca's at-least-once webhook delivery
        means the same canceled event may arrive twice (Codex PR #102
        finding 1). An order can only reach one terminal cancel state,
        so ``client_order_id`` alone is the dedup key (no ``cancel_id``
        analog to ``execution_id`` is delivered by Alpaca).
        """
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM cancellations WHERE client_order_id = ? LIMIT 1",
                (client_order_id,),
            )
            if cur.fetchone() is not None:
                return False
            self._conn.execute(
                "INSERT INTO cancellations (client_order_id, reason, recorded_at) VALUES (?, ?, ?)",
                (client_order_id, reason, time.time()),
            )
            self._conn.commit()
            return True

    # --- read surface -----------------------------------------------------

    def attribution(self, client_order_id: str) -> tuple[str, str] | None:
        """Resolve ``client_order_id`` → ``(cycle_id, decision_run_id)``
        from the submissions table. Returns ``None`` if the order id
        was not registered (this process did not submit it, OR the
        journal pre-dates the order, OR data was pruned).
        """
        cur = self._conn.execute(
            "SELECT cycle_id, decision_run_id FROM submissions WHERE client_order_id = ?",
            (client_order_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return (str(row[0]), str(row[1]))

    def make_resolvers(self) -> tuple[CycleIdResolver, CycleIdResolver]:
        """Build the two callables :func:`caqrs.execution.alpaca_stream.consume`
        consumes for cycle attribution. Both callables share the same
        attribution lookup; the second extracts ``decision_run_id``.

        Convenience over operator-built resolvers; bypassable when a
        custom resolution path is needed (e.g. multi-process registry,
        cross-host journal).
        """

        def _cycle_resolver(client_order_id: str) -> str | None:
            attr = self.attribution(client_order_id)
            return attr[0] if attr is not None else None

        def _decision_resolver(client_order_id: str) -> str | None:
            attr = self.attribution(client_order_id)
            return attr[1] if attr is not None else None

        return (_cycle_resolver, _decision_resolver)
