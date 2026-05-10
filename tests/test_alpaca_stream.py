"""Unit tests for the Alpaca trade-update websocket subscriber.

ADR-0006 step 1 / step 2 dispatch:

- **Step 1** (this commit): every test below is decorated
  ``@pytest.mark.xfail(strict=True, reason="impl pending — Alpaca stream")``
  and exercises the public surface of
  :mod:`caqrs.execution.alpaca_stream`. ``decode_trade_update`` and
  ``consume`` raise ``NotImplementedError`` so all tests xfail
  cleanly.
- **Step 2** (next commit): bodies implemented; xfail markers
  removed; the four NFR-LIVE-BROKER-7 deferred event halves
  (``BROKER_LIVE_FILLED`` / ``BROKER_LIVE_CANCELLED``) are now
  exercised end-to-end.

Tests cover the decoder (pure function over dicts) and the consume
loop (async iterator → EventLog emission). The actual websocket
client wiring is the operator's concern and not tested here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest

from caqrs.execution.alpaca_stream import (
    AlpacaTradeUpdate,
    TradeUpdateKind,
    consume,
    decode_trade_update,
)
from caqrs.execution.live_broker_journal import LiveBrokerJournal
from caqrs.orchestrator import CycleEventKind, EventLog


def _fill_msg(execution_id: str | None = "exec-1") -> dict[str, object]:
    data: dict[str, object] = {
        "event": "fill",
        "order": {
            "id": "venue-uuid-1",
            "client_order_id": "abc123",
            "symbol": "AAPL",
            "side": "buy",
        },
        "qty": "10",
        "price": "180.50",
    }
    if execution_id is not None:
        data["execution_id"] = execution_id
    return {"stream": "trade_updates", "data": data}


def _partial_fill_msg() -> dict[str, object]:
    msg = _fill_msg()
    msg["data"]["event"] = "partial_fill"  # type: ignore[index]
    msg["data"]["qty"] = "3"  # type: ignore[index]
    return msg


def _canceled_msg() -> dict[str, object]:
    return {
        "stream": "trade_updates",
        "data": {
            "event": "canceled",
            "order": {
                "id": "venue-uuid-1",
                "client_order_id": "abc123",
                "symbol": "AAPL",
                "side": "buy",
            },
            "reason": "operator-initiated cancel",
        },
    }


def _unrelated_msg() -> dict[str, object]:
    return {
        "stream": "trade_updates",
        "data": {
            "event": "new",
            "order": {
                "id": "venue-uuid-2",
                "client_order_id": "xyz",
                "symbol": "MSFT",
                "side": "sell",
            },
        },
    }


def test_decode_trade_update_handles_fill_message() -> None:
    parsed = decode_trade_update(_fill_msg())
    assert parsed is not None
    assert parsed.kind is TradeUpdateKind.FILL
    assert parsed.order_id == "venue-uuid-1"
    assert parsed.client_order_id == "abc123"
    assert parsed.symbol == "AAPL"
    assert parsed.side == "buy"
    assert parsed.filled_qty == Decimal("10")
    assert parsed.filled_avg_price == Decimal("180.50")


def test_decode_trade_update_handles_partial_fill_message() -> None:
    parsed = decode_trade_update(_partial_fill_msg())
    assert parsed is not None
    assert parsed.kind is TradeUpdateKind.PARTIAL_FILL
    assert parsed.filled_qty == Decimal("3")


def test_decode_trade_update_handles_canceled_message() -> None:
    parsed = decode_trade_update(_canceled_msg())
    assert parsed is not None
    assert parsed.kind is TradeUpdateKind.CANCELED
    assert parsed.reason == "operator-initiated cancel"
    assert parsed.filled_qty is None


def test_decode_trade_update_returns_none_for_unrelated_event() -> None:
    """Events outside the four taxonomy-mapped kinds (new,
    done_for_day, replaced, ...) MUST decode to None — the cycle
    log only records terminal-state transitions."""
    assert decode_trade_update(_unrelated_msg()) is None


async def _async_iter(messages: list[dict[str, object]]) -> AsyncIterator[dict[str, object]]:
    for m in messages:
        yield m


@pytest.mark.asyncio
async def test_consume_emits_broker_live_filled_for_fill_event() -> None:
    """A fill event with a known client_order_id MUST emit
    BROKER_LIVE_FILLED with the resolved cycle_id, decision_run_id,
    and the fill qty + price from the venue."""
    log = EventLog()
    cycle_map = {"abc123": "cycle-X"}
    decision_map = {"abc123": "decision-Y"}
    await consume(
        _async_iter([_fill_msg()]),
        event_log=log,
        cycle_id_resolver=cycle_map.get,
        decision_run_id_resolver=decision_map.get,
    )
    filled = log.filter_by_kind(CycleEventKind.BROKER_LIVE_FILLED)
    assert len(filled) == 1
    payload = filled[0].payload
    assert filled[0].cycle_id == "cycle-X"
    assert payload["order_id"] == "venue-uuid-1"
    assert payload["filled_qty"] == "10"
    assert payload["filled_avg_price_usd"] == "180.50"
    assert payload["is_partial"] is False


@pytest.mark.asyncio
async def test_consume_emits_broker_live_cancelled_for_canceled_event() -> None:
    log = EventLog()
    await consume(
        _async_iter([_canceled_msg()]),
        event_log=log,
        cycle_id_resolver=lambda _: "cycle-X",
        decision_run_id_resolver=lambda _: "decision-Y",
    )
    cancelled = log.filter_by_kind(CycleEventKind.BROKER_LIVE_CANCELLED)
    assert len(cancelled) == 1
    assert cancelled[0].payload["reason"] == "operator-initiated cancel"


@pytest.mark.asyncio
async def test_consume_drops_unresolvable_client_order_ids() -> None:
    """If the resolver returns None (this process didn't submit the
    order), the trade update MUST be dropped without emitting a
    cycle event — there's no cycle to attribute it to."""
    log = EventLog()
    await consume(
        _async_iter([_fill_msg()]),
        event_log=log,
        cycle_id_resolver=lambda _: None,
        decision_run_id_resolver=lambda _: None,
    )
    assert log.filter_by_kind(CycleEventKind.BROKER_LIVE_FILLED) == ()


@pytest.mark.asyncio
async def test_consume_silently_skips_unrelated_events() -> None:
    """Non-fill/cancel events (new, done_for_day, ...) MUST be
    silently dropped — the cycle log only records terminal
    transitions."""
    log = EventLog()
    await consume(
        _async_iter([_unrelated_msg(), _fill_msg()]),
        event_log=log,
        cycle_id_resolver=lambda _: "cycle-X",
        decision_run_id_resolver=lambda _: "decision-Y",
    )
    # Only the fill emitted; the 'new' message was dropped.
    assert len(log.filter_by_kind(CycleEventKind.BROKER_LIVE_FILLED)) == 1


def test_alpaca_trade_update_dataclass_is_frozen() -> None:
    """Frozen-by-design — once decoded, AlpacaTradeUpdate is an
    immutable record so multi-step processing can't mutate the
    parsed venue event."""
    update = AlpacaTradeUpdate(
        kind=TradeUpdateKind.FILL,
        order_id="x",
        client_order_id="y",
        symbol="AAPL",
        side="buy",
    )
    with pytest.raises(AttributeError):
        update.symbol = "MSFT"  # type: ignore[misc]


# === Codex audit 2026-05-10 regressions =================================


def test_decode_trade_update_returns_none_on_malformed_qty() -> None:
    """Codex audit blocker 1: a malformed numeric field MUST NOT raise
    out of decode_trade_update — the stream loop relies on returning
    None to keep going."""
    msg = _fill_msg()
    msg["data"]["qty"] = "not-a-number"  # type: ignore[index]
    assert decode_trade_update(msg) is None


def test_decode_trade_update_returns_none_when_qty_missing_on_fill() -> None:
    """Codex audit blocker 2: a fill message missing qty MUST decode
    to None rather than emitting a "0" fill that corrupts downstream
    position/PnL accounting."""
    msg = _fill_msg()
    del msg["data"]["qty"]  # type: ignore[attr-defined]
    assert decode_trade_update(msg) is None


def test_decode_trade_update_returns_none_when_qty_non_positive() -> None:
    """A zero or negative qty is not a valid fill."""
    msg = _fill_msg()
    msg["data"]["qty"] = "0"  # type: ignore[index]
    assert decode_trade_update(msg) is None
    msg["data"]["qty"] = "-1"  # type: ignore[index]
    assert decode_trade_update(msg) is None


@pytest.mark.asyncio
async def test_consume_continues_past_malformed_message() -> None:
    """Codex audit blocker 1: one malformed message in a batch MUST NOT
    stop later valid fills from being consumed and emitted."""
    log = EventLog()
    bad = _fill_msg()
    bad["data"]["qty"] = "not-a-number"  # type: ignore[index]
    good = _fill_msg()
    good["data"]["order"]["client_order_id"] = "good-id"  # type: ignore[index]
    await consume(
        _async_iter([bad, good]),
        event_log=log,
        cycle_id_resolver=lambda _: "cycle-X",
        decision_run_id_resolver=lambda _: "decision-Y",
    )
    filled = log.filter_by_kind(CycleEventKind.BROKER_LIVE_FILLED)
    assert len(filled) == 1
    assert filled[0].payload["client_order_id"] == "good-id"


# === Journal wire-in (PR #100 majors 3+4 follow-through) ===========


@pytest.mark.asyncio
async def test_consume_with_journal_records_fills_and_cancels(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """When journal= is passed, consume MUST persist record_fill +
    record_cancel calls per event AND derive cycle_id_resolver +
    decision_run_id_resolver from journal.make_resolvers()."""

    path = Path(str(tmp_path)) / "j.sqlite"
    log = EventLog()
    with LiveBrokerJournal(path=path) as journal:
        # Pre-register one submission so the resolver can attribute.
        journal.register_submission(
            client_order_id="abc123",
            cycle_id="cycle-X",
            decision_run_id="decision-Y",
            order_id="venue-uuid-1",
            idempotency_key="full-64-key",
            symbol="AAPL",
            side="buy",
            qty=Decimal("10"),
        )
        await consume(
            _async_iter([_fill_msg(), _canceled_msg()]),
            event_log=log,
            journal=journal,
        )
        # Cycle event log shows both events.
        assert len(log.filter_by_kind(CycleEventKind.BROKER_LIVE_FILLED)) == 1
        assert len(log.filter_by_kind(CycleEventKind.BROKER_LIVE_CANCELLED)) == 1
        # Journal also persisted them — proven by direct sqlite read.
        cur = journal._conn.execute("SELECT COUNT(*) FROM fills")
        assert cur.fetchone()[0] == 1
        cur = journal._conn.execute("SELECT COUNT(*) FROM cancellations")
        assert cur.fetchone()[0] == 1


@pytest.mark.asyncio
async def test_consume_without_journal_and_without_resolvers_raises() -> None:
    """consume() requires either journal= OR explicit resolvers.
    Neither → ValueError; better fail-fast than silently drop every
    event."""
    log = EventLog()
    with pytest.raises(ValueError, match="journal="):
        await consume(_async_iter([]), event_log=log)


# === execution_id dedup (PR #101 Codex finding 2 follow-through) ===


def test_decode_trade_update_extracts_execution_id() -> None:
    """Codex PR #101 finding 2: when Alpaca's trade-update message
    carries an ``execution_id``, the decoder MUST surface it as
    ``AlpacaTradeUpdate.fill_id`` so :func:`consume` can pass it to
    :meth:`LiveBrokerJournal.record_fill` for at-least-once dedup."""
    parsed = decode_trade_update(_fill_msg(execution_id="exec-abc"))
    assert parsed is not None
    assert parsed.fill_id == "exec-abc"


def test_decode_trade_update_fill_id_none_when_absent() -> None:
    """Backward compat: when ``execution_id`` is missing the decoder
    MUST set ``fill_id=None`` (the journal still inserts audit-only)."""
    parsed = decode_trade_update(_fill_msg(execution_id=None))
    assert parsed is not None
    assert parsed.fill_id is None


def test_decode_trade_update_fill_id_none_on_empty_execution_id() -> None:
    """Codex PR #102 nitpick: an empty-string ``execution_id`` falls
    back to audit-only insert rather than dropping the message — losing
    a real fill is more dangerous than recording one without dedup."""
    msg = _fill_msg(execution_id=None)
    msg["data"]["execution_id"] = ""  # type: ignore[index]
    parsed = decode_trade_update(msg)
    assert parsed is not None
    assert parsed.fill_id is None


@pytest.mark.asyncio
async def test_consume_passes_execution_id_to_journal_record_fill(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """consume() MUST pass ``decoded.fill_id`` through to
    ``journal.record_fill(fill_id=...)`` so the journal's UNIQUE
    constraint can suppress duplicate webhook deliveries."""

    path = Path(str(tmp_path)) / "j.sqlite"
    log = EventLog()
    with LiveBrokerJournal(path=path) as journal:
        journal.register_submission(
            client_order_id="abc123",
            cycle_id="cycle-X",
            decision_run_id="decision-Y",
            order_id="venue-uuid-1",
            idempotency_key="full-key",
            symbol="AAPL",
            side="buy",
            qty=Decimal("10"),
        )
        await consume(
            _async_iter([_fill_msg(execution_id="exec-1")]),
            event_log=log,
            journal=journal,
        )
        cur = journal._conn.execute("SELECT fill_id FROM fills")
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "exec-1"


@pytest.mark.asyncio
async def test_consume_suppresses_duplicate_fill_emission(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """When the same (client_order_id, execution_id) arrives twice,
    record_fill returns False on the second call. consume() MUST
    capture the False and skip ``event_log.append`` so downstream
    cycle consumers see exactly one BROKER_LIVE_FILLED per fill."""

    path = Path(str(tmp_path)) / "j.sqlite"
    log = EventLog()
    with LiveBrokerJournal(path=path) as journal:
        journal.register_submission(
            client_order_id="abc123",
            cycle_id="cycle-X",
            decision_run_id="decision-Y",
            order_id="venue-uuid-1",
            idempotency_key="full-key",
            symbol="AAPL",
            side="buy",
            qty=Decimal("10"),
        )
        # Two identical fill messages with the same execution_id.
        msg1 = _fill_msg(execution_id="exec-1")
        msg2 = _fill_msg(execution_id="exec-1")
        await consume(_async_iter([msg1, msg2]), event_log=log, journal=journal)
        # Journal: one row (UNIQUE collapsed the duplicate).
        cur = journal._conn.execute("SELECT COUNT(*) FROM fills")
        assert cur.fetchone()[0] == 1
        # EventLog: one event (consume MUST skip on record_fill==False).
        assert len(log.filter_by_kind(CycleEventKind.BROKER_LIVE_FILLED)) == 1


@pytest.mark.asyncio
async def test_consume_suppresses_duplicate_cancel_emission(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """Codex PR #102 finding 1: when the same client_order_id's
    canceled event arrives twice, record_cancel MUST return False on
    the second call and consume MUST skip the second
    BROKER_LIVE_CANCELLED emission. Without this fix, at-least-once
    webhook delivery yielded duplicate terminal events downstream."""

    path = Path(str(tmp_path)) / "j.sqlite"
    log = EventLog()
    with LiveBrokerJournal(path=path) as journal:
        journal.register_submission(
            client_order_id="abc123",
            cycle_id="cycle-X",
            decision_run_id="decision-Y",
            order_id="venue-uuid-1",
            idempotency_key="full-key",
            symbol="AAPL",
            side="buy",
            qty=Decimal("10"),
        )
        msg1 = _canceled_msg()
        msg2 = _canceled_msg()
        await consume(_async_iter([msg1, msg2]), event_log=log, journal=journal)
        # Journal: one row only.
        cur = journal._conn.execute("SELECT COUNT(*) FROM cancellations")
        assert cur.fetchone()[0] == 1
        # EventLog: one event only.
        assert len(log.filter_by_kind(CycleEventKind.BROKER_LIVE_CANCELLED)) == 1


@pytest.mark.asyncio
async def test_consume_emits_both_when_execution_ids_differ(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """Two genuine partial fills with distinct execution_ids MUST
    each emit BROKER_LIVE_FILLED — dedup keys on (client_order_id,
    execution_id), not on quantity / price."""

    path = Path(str(tmp_path)) / "j.sqlite"
    log = EventLog()
    with LiveBrokerJournal(path=path) as journal:
        journal.register_submission(
            client_order_id="abc123",
            cycle_id="cycle-X",
            decision_run_id="decision-Y",
            order_id="venue-uuid-1",
            idempotency_key="full-key",
            symbol="AAPL",
            side="buy",
            qty=Decimal("10"),
        )
        msg1 = _fill_msg(execution_id="exec-1")
        msg2 = _fill_msg(execution_id="exec-2")
        await consume(_async_iter([msg1, msg2]), event_log=log, journal=journal)
        cur = journal._conn.execute("SELECT COUNT(*) FROM fills")
        assert cur.fetchone()[0] == 2
        assert len(log.filter_by_kind(CycleEventKind.BROKER_LIVE_FILLED)) == 2


@pytest.mark.asyncio
async def test_consume_explicit_resolver_takes_precedence_over_journal(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """When BOTH journal and explicit resolvers are passed, explicit
    resolvers win. Use case: tests that want to override attribution
    while still recording to the journal for restart-survival proofs."""

    path = Path(str(tmp_path)) / "j.sqlite"
    log = EventLog()
    with LiveBrokerJournal(path=path) as journal:
        # Journal has NO submission registered → its resolvers return None.
        # Explicit resolver returns a value → consume should use it.
        await consume(
            _async_iter([_fill_msg()]),
            event_log=log,
            journal=journal,
            cycle_id_resolver=lambda _: "explicit-cycle",
            decision_run_id_resolver=lambda _: "explicit-decision",
        )
        # Event emitted with explicit attribution.
        filled = log.filter_by_kind(CycleEventKind.BROKER_LIVE_FILLED)
        assert len(filled) == 1
        assert filled[0].cycle_id == "explicit-cycle"
        # Journal still received the fill record (durability still on).
        cur = journal._conn.execute("SELECT COUNT(*) FROM fills")
        assert cur.fetchone()[0] == 1
