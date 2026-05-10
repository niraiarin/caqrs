"""Tests for the durable live-broker journal.

Addresses Codex audit 2026-05-10 PR #99 majors 3 + 4 (late-fill
persistence + durable resolver registry). The journal is
SQLite-backed; tests use ``Path(":memory:")`` plus on-disk
temp paths for restart simulation.
"""

from __future__ import annotations

import sqlite3
import threading
from decimal import Decimal
from pathlib import Path

import pytest

from caqrs.execution.live_broker_journal import LiveBrokerJournal


def _make_journal(tmp_path: Path | None = None) -> LiveBrokerJournal:
    path = tmp_path / "journal.sqlite" if tmp_path is not None else Path(":memory:")
    return LiveBrokerJournal(path=path)


# --- attribution: register + look up --------------------------------------


def test_register_submission_then_attribution_returns_recorded_pair() -> None:
    """register_submission MUST persist the cycle_id + decision_run_id;
    attribution MUST return the same pair."""
    with _make_journal() as journal:
        journal.register_submission(
            client_order_id="abc-48-chars",
            cycle_id="cycle-X",
            decision_run_id="decision-Y",
            order_id="venue-uuid",
            idempotency_key="full-64-char-hex",
            symbol="AAPL",
            side="buy",
            qty=Decimal("10"),
        )
        assert journal.attribution("abc-48-chars") == ("cycle-X", "decision-Y")


def test_attribution_returns_none_for_unknown_client_order_id() -> None:
    """Unknown ids return None — the resolver-side semantics
    consume() expects."""
    with _make_journal() as journal:
        assert journal.attribution("never-submitted") is None


def test_register_submission_rejects_duplicate_client_order_id() -> None:
    """Re-registering the same id is a programmer error
    (sha256 collisions negligible). The journal MUST surface this
    as IntegrityError so the operator notices."""
    with _make_journal() as journal:
        journal.register_submission(
            client_order_id="dup",
            cycle_id="c1",
            decision_run_id="d1",
            order_id="o1",
            idempotency_key="key1",
            symbol="AAPL",
            side="buy",
            qty=Decimal("1"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            journal.register_submission(
                client_order_id="dup",
                cycle_id="c2",
                decision_run_id="d2",
                order_id="o2",
                idempotency_key="key2",
                symbol="AAPL",
                side="sell",
                qty=Decimal("2"),
            )


# --- fills ----------------------------------------------------------------


def test_record_fill_returns_true_on_first_insert() -> None:
    with _make_journal() as journal:
        journal.register_submission(
            client_order_id="abc",
            cycle_id="c",
            decision_run_id="d",
            order_id="o",
            idempotency_key="k",
            symbol="AAPL",
            side="buy",
            qty=Decimal("10"),
        )
        new = journal.record_fill(
            client_order_id="abc",
            qty=Decimal("10"),
            fill_price_usd=Decimal("180.50"),
            is_partial=False,
            fill_id="exec-1",
        )
        assert new is True


def test_record_fill_dedups_on_duplicate_client_order_and_fill_id() -> None:
    """Codex audit at-least-once delivery concern: the journal MUST
    collapse duplicate fill events keyed on (client_order_id, fill_id)."""
    with _make_journal() as journal:
        journal.register_submission(
            client_order_id="abc",
            cycle_id="c",
            decision_run_id="d",
            order_id="o",
            idempotency_key="k",
            symbol="AAPL",
            side="buy",
            qty=Decimal("10"),
        )
        first = journal.record_fill(
            client_order_id="abc",
            qty=Decimal("10"),
            fill_price_usd=Decimal("180.50"),
            is_partial=False,
            fill_id="exec-1",
        )
        second = journal.record_fill(
            client_order_id="abc",
            qty=Decimal("10"),
            fill_price_usd=Decimal("180.50"),
            is_partial=False,
            fill_id="exec-1",
        )
        assert first is True
        assert second is False


def test_record_fill_inserts_when_fill_id_is_none() -> None:
    """When the venue doesn't supply a fill_id, the journal can't
    dedup — every call inserts. Documented as the weaker key in
    PR-99's verifier report."""
    with _make_journal() as journal:
        journal.register_submission(
            client_order_id="abc",
            cycle_id="c",
            decision_run_id="d",
            order_id="o",
            idempotency_key="k",
            symbol="AAPL",
            side="buy",
            qty=Decimal("3"),
        )
        a = journal.record_fill(
            client_order_id="abc",
            qty=Decimal("1"),
            fill_price_usd=Decimal("180"),
            is_partial=True,
        )
        b = journal.record_fill(
            client_order_id="abc",
            qty=Decimal("2"),
            fill_price_usd=Decimal("180.50"),
            is_partial=True,
        )
        assert a is True
        assert b is True


# --- cancellations --------------------------------------------------------


def test_record_cancel_with_reason() -> None:
    with _make_journal() as journal:
        journal.register_submission(
            client_order_id="abc",
            cycle_id="c",
            decision_run_id="d",
            order_id="o",
            idempotency_key="k",
            symbol="AAPL",
            side="buy",
            qty=Decimal("10"),
        )
        journal.record_cancel(
            client_order_id="abc",
            reason="operator-initiated cancel",
        )


def test_record_cancel_with_null_reason() -> None:
    """Codex review accepted that reason can be None when the venue
    omits it. The journal MUST accept None without coercion."""
    with _make_journal() as journal:
        journal.register_submission(
            client_order_id="abc",
            cycle_id="c",
            decision_run_id="d",
            order_id="o",
            idempotency_key="k",
            symbol="AAPL",
            side="buy",
            qty=Decimal("10"),
        )
        journal.record_cancel(client_order_id="abc", reason=None)


@pytest.mark.xfail(strict=True, reason="impl pending — Task #15 cancel-side dedup")
def test_record_cancel_dedups_duplicate_delivery() -> None:
    """Codex PR #102 finding 1: Alpaca's trade-update stream delivers
    cancellations at-least-once. record_cancel MUST return True on
    first call and False on duplicate (same client_order_id) so the
    stream consumer can suppress the duplicate BROKER_LIVE_CANCELLED
    emission. An order can only reach one terminal cancel state, so
    client_order_id alone is the dedup key."""
    with _make_journal() as journal:
        journal.register_submission(
            client_order_id="abc",
            cycle_id="c",
            decision_run_id="d",
            order_id="o",
            idempotency_key="k",
            symbol="AAPL",
            side="buy",
            qty=Decimal("10"),
        )
        first = journal.record_cancel(
            client_order_id="abc",
            reason="operator-initiated cancel",
        )
        second = journal.record_cancel(
            client_order_id="abc",
            reason="operator-initiated cancel",
        )
        assert first is True
        assert second is False
        # Only one row persisted.
        cur = journal._conn.execute("SELECT COUNT(*) FROM cancellations")
        assert cur.fetchone()[0] == 1


@pytest.mark.xfail(strict=True, reason="impl pending — Task #15 cancel-side dedup")
def test_record_cancel_distinct_orders_both_recorded() -> None:
    """Two genuine cancellations for different orders MUST both
    insert and both return True — dedup keys on client_order_id, not
    on time or reason."""
    with _make_journal() as journal:
        for client_order_id in ("abc", "xyz"):
            journal.register_submission(
                client_order_id=client_order_id,
                cycle_id="c",
                decision_run_id="d",
                order_id=f"o-{client_order_id}",
                idempotency_key=f"k-{client_order_id}",
                symbol="AAPL",
                side="buy",
                qty=Decimal("1"),
            )
        a = journal.record_cancel(client_order_id="abc", reason=None)
        b = journal.record_cancel(client_order_id="xyz", reason=None)
        assert a is True
        assert b is True


# --- restart simulation ---------------------------------------------------


def test_journal_attribution_survives_close_and_reopen(tmp_path: Path) -> None:
    """Codex audit major 4: the resolver MUST survive process restart.
    Open journal, register, close. Reopen against the same path,
    attribution MUST still return the recorded pair — proving the
    durable contract."""
    path = tmp_path / "j.sqlite"
    journal_a = LiveBrokerJournal(path=path)
    journal_a.register_submission(
        client_order_id="abc",
        cycle_id="c",
        decision_run_id="d",
        order_id="o",
        idempotency_key="k",
        symbol="AAPL",
        side="buy",
        qty=Decimal("10"),
    )
    journal_a.close()
    # Reopen — separate Connection, same database file.
    journal_b = LiveBrokerJournal(path=path)
    assert journal_b.attribution("abc") == ("c", "d")
    journal_b.close()


# --- make_resolvers convenience ------------------------------------------


def test_make_resolvers_returns_pair_consume_can_use() -> None:
    """make_resolvers is the convenience over building the two
    callables alpaca_stream.consume() expects. Each callable maps
    client_order_id → str | None."""
    with _make_journal() as journal:
        journal.register_submission(
            client_order_id="known",
            cycle_id="cycle-X",
            decision_run_id="decision-Y",
            order_id="o",
            idempotency_key="k",
            symbol="AAPL",
            side="buy",
            qty=Decimal("1"),
        )
        cycle_resolver, decision_resolver = journal.make_resolvers()
        assert cycle_resolver("known") == "cycle-X"
        assert decision_resolver("known") == "decision-Y"
        assert cycle_resolver("unknown") is None
        assert decision_resolver("unknown") is None


# === Codex audit 2026-05-10 regressions ===========================


def test_fills_survive_close_and_reopen(tmp_path: Path) -> None:
    """Codex audit finding 3: restart coverage previously only checked
    submissions. Fills MUST also survive close + reopen — that's the
    whole point of late-fill persistence."""
    path = tmp_path / "j.sqlite"
    j_a = LiveBrokerJournal(path=path)
    j_a.register_submission(
        client_order_id="abc",
        cycle_id="c",
        decision_run_id="d",
        order_id="o",
        idempotency_key="k",
        symbol="AAPL",
        side="buy",
        qty=Decimal("10"),
    )
    j_a.record_fill(
        client_order_id="abc",
        qty=Decimal("10"),
        fill_price_usd=Decimal("180.50"),
        is_partial=False,
        fill_id="exec-1",
    )
    j_a.close()

    j_b = LiveBrokerJournal(path=path)
    # Re-attempting the same fill_id MUST hit the UNIQUE constraint
    # (proves the row persisted across reopen) and return False.
    duplicate = j_b.record_fill(
        client_order_id="abc",
        qty=Decimal("10"),
        fill_price_usd=Decimal("180.50"),
        is_partial=False,
        fill_id="exec-1",
    )
    assert duplicate is False
    j_b.close()


def test_cancellations_survive_close_and_reopen(tmp_path: Path) -> None:
    """Cancellations MUST also persist; verified via direct SQLite
    introspection because the public API is write-only for this
    table."""
    path = tmp_path / "j.sqlite"
    j_a = LiveBrokerJournal(path=path)
    j_a.register_submission(
        client_order_id="abc",
        cycle_id="c",
        decision_run_id="d",
        order_id="o",
        idempotency_key="k",
        symbol="AAPL",
        side="buy",
        qty=Decimal("10"),
    )
    j_a.record_cancel(client_order_id="abc", reason="operator-initiated")
    j_a.close()

    # Direct sqlite read on the persisted file.
    conn = sqlite3.connect(str(path))
    rows = conn.execute("SELECT client_order_id, reason FROM cancellations").fetchall()
    conn.close()
    assert rows == [("abc", "operator-initiated")]


def test_journal_refuses_to_open_newer_schema_version(tmp_path: Path) -> None:
    """Codex audit finding 2: opening a journal whose stored
    user_version is **higher** than this CAQRS build supports MUST
    refuse — running against a forward-incompatible journal could
    silently misinterpret rows."""
    path = tmp_path / "j.sqlite"
    # Pre-create a database with user_version=99 (unsupported).
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="newer than this CAQRS build"):
        LiveBrokerJournal(path=path)


def test_journal_concurrent_writes_do_not_raise(tmp_path: Path) -> None:
    """Codex audit finding 1: a single journal instance MUST tolerate
    concurrent writes from multiple threads. The internal lock
    serialises connection access; check_same_thread=False allows
    cross-thread reuse."""

    path = tmp_path / "j.sqlite"
    journal = LiveBrokerJournal(path=path)
    errors: list[BaseException] = []

    def _worker(idx: int) -> None:
        try:
            journal.register_submission(
                client_order_id=f"id-{idx}",
                cycle_id=f"c-{idx}",
                decision_run_id=f"d-{idx}",
                order_id=f"o-{idx}",
                idempotency_key=f"k-{idx}",
                symbol="AAPL",
                side="buy",
                qty=Decimal("1"),
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    # All 10 submissions persisted.
    for i in range(10):
        assert journal.attribution(f"id-{i}") == (f"c-{i}", f"d-{i}")
    journal.close()
