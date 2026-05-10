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

import pytest

from caqrs.execution.alpaca_stream import (
    AlpacaTradeUpdate,
    TradeUpdateKind,
    consume,
    decode_trade_update,
)
from caqrs.orchestrator import CycleEventKind, EventLog


def _fill_msg() -> dict[str, object]:
    return {
        "stream": "trade_updates",
        "data": {
            "event": "fill",
            "order": {
                "id": "venue-uuid-1",
                "client_order_id": "abc123",
                "symbol": "AAPL",
                "side": "buy",
            },
            "qty": "10",
            "price": "180.50",
        },
    }


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
