"""Alpaca trade-update websocket subscriber.

Per ADR-0008 §NFR-LIVE-BROKER-7 and ADR-0009 §"Per-NFR mapping
(NFR-LIVE-BROKER-7)", the live broker emits ``BROKER_LIVE_FILLED`` /
``BROKER_LIVE_CANCELLED`` events when the venue confirms fills or
cancellations. PR #98 left these emissions deferred behind the
``SUBMITTED`` placeholder state; this module fills the gap by
subscribing to Alpaca's trade-update websocket
(``wss://paper-api.alpaca.markets/stream`` for paper,
``wss://api.alpaca.markets/stream`` for live) and translating each
event into the corresponding ``CycleEvent``.

Architecture: the actual websocket connection (with reconnect
handling, heartbeat, etc.) is the operator's concern — this module
exposes :func:`consume` as a pure async function over an
``AsyncIterator[dict]`` so tests can drive it with synthetic message
streams while production code wraps a real ``websockets`` client.

Event-to-CycleEvent mapping per Alpaca's documented trade-update
shape:

- ``event=fill`` or ``event=partial_fill`` →
  :func:`broker_live_filled_event` with the actual ``filled_qty`` and
  ``filled_avg_price`` from the message.
- ``event=canceled`` or ``event=rejected`` →
  :func:`broker_live_cancelled_event` with the venue's reason string.
- Unrelated events (``new``, ``done_for_day``, etc.) → silently
  dropped; the cycle log only records terminal-state transitions.

Cycle attribution: each trade-update carries the ``client_order_id``
the broker set at submission time; callers provide a
``cycle_id_resolver`` that maps ``client_order_id`` → ``cycle_id``
so emitted events join correctly with their originating cycle. When
the resolver returns ``None`` (order id not seen by this process),
the event is dropped with a warning to ``stderr``.

Step 1 / Step 2 dispatch (ADR-0006): bodies raise
``NotImplementedError`` in step 1; step 2 fills them in.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import TYPE_CHECKING

from caqrs.orchestrator.event_log import EventLog

if TYPE_CHECKING:
    from caqrs.execution.live_broker_journal import LiveBrokerJournal
from caqrs.orchestrator.events import (
    broker_live_cancelled_event,
    broker_live_filled_event,
)


class TradeUpdateKind(StrEnum):
    """Subset of Alpaca's documented trade_update events that map
    to ``BROKER_LIVE_*`` cycle events. Other events (``new``,
    ``done_for_day``, ``replaced``, ...) are not represented here —
    :func:`decode_trade_update` returns ``None`` for them."""

    FILL = "fill"
    PARTIAL_FILL = "partial_fill"
    CANCELED = "canceled"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class AlpacaTradeUpdate:
    """One parsed trade-update event from Alpaca's websocket stream.

    ``filled_qty`` / ``filled_avg_price`` are populated only on
    ``FILL`` / ``PARTIAL_FILL`` events; on ``CANCELED`` / ``REJECTED``
    they are ``None``. ``reason`` is populated on terminal-state
    rejections so the BROKER_LIVE_* event payload can record the
    venue's stated cause verbatim.
    """

    kind: TradeUpdateKind
    order_id: str
    client_order_id: str
    symbol: str
    side: str
    filled_qty: Decimal | None = None
    filled_avg_price: Decimal | None = None
    reason: str | None = None
    fill_id: str | None = None
    """Alpaca's ``execution_id`` for FILL / PARTIAL_FILL events,
    populated when the venue supplies one. Used by
    :meth:`LiveBrokerJournal.record_fill` to dedup at-least-once
    webhook deliveries (Codex PR #101 finding 2)."""


def decode_trade_update(raw: dict[str, object]) -> AlpacaTradeUpdate | None:  # noqa: PLR0911
    """Parse one raw trade-update message into an
    :class:`AlpacaTradeUpdate`, or return ``None`` if the message is
    not one of the four taxonomy-mapped events.

    Per Alpaca's docs, the message shape is::

        {"stream": "trade_updates", "data": {
            "event": "fill" | "partial_fill" | "canceled" | ...,
            "order": {
                "id": "...", "client_order_id": "...",
                "symbol": "AAPL", "side": "buy", ...
            },
            "qty": "10", "price": "180.05",  # fill / partial_fill
            ...
        }}

    Robust to missing optional fields. Returns ``None`` for any
    ``data.event`` that isn't in :class:`TradeUpdateKind`.
    """
    data = raw.get("data")
    if not isinstance(data, dict):
        return None
    event_name = data.get("event")
    if not isinstance(event_name, str):
        return None
    try:
        kind = TradeUpdateKind(event_name)
    except ValueError:
        return None
    order = data.get("order")
    if not isinstance(order, dict):
        return None
    order_id = str(order.get("id", ""))
    client_order_id = str(order.get("client_order_id", ""))
    symbol = str(order.get("symbol", ""))
    side = str(order.get("side", ""))
    if not (order_id and client_order_id and symbol and side):
        return None
    filled_qty: Decimal | None = None
    filled_avg_price: Decimal | None = None
    fill_id: str | None = None
    if kind in {TradeUpdateKind.FILL, TradeUpdateKind.PARTIAL_FILL}:
        # Codex audit 2026-05-10 blockers 1 + 2: qty/price are
        # mandatory on fill events. Drop the message if either is
        # missing, unparseable, or non-positive — otherwise we'd
        # emit "0" fills and corrupt downstream position/PnL state.
        qty_raw = data.get("qty")
        price_raw = data.get("price")
        if qty_raw is None or price_raw is None:
            return None
        try:
            filled_qty = Decimal(str(qty_raw))
            filled_avg_price = Decimal(str(price_raw))
        except (InvalidOperation, ValueError):
            return None
        if filled_qty <= 0 or filled_avg_price <= 0:
            return None
        # Codex PR #101 finding 2: surface execution_id when present
        # so the journal can dedup at-least-once webhook delivery on
        # (client_order_id, fill_id). Absence stays None (audit-only
        # insert, no dedup) for back-compat with brokers that omit it.
        execution_id_raw = data.get("execution_id")
        if isinstance(execution_id_raw, str) and execution_id_raw:
            fill_id = execution_id_raw
    reason: str | None = None
    if kind in {TradeUpdateKind.CANCELED, TradeUpdateKind.REJECTED}:
        reason_raw = data.get("reason")
        if isinstance(reason_raw, str):
            reason = reason_raw
    return AlpacaTradeUpdate(
        kind=kind,
        order_id=order_id,
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        filled_qty=filled_qty,
        filled_avg_price=filled_avg_price,
        reason=reason,
        fill_id=fill_id,
    )


CycleIdResolver = Callable[[str], str | None]
"""``client_order_id`` → ``cycle_id`` map. The broker registers each
submission's ``client_order_id`` against the cycle it ran in;
:func:`consume` looks up the cycle attribution this way so emitted
events join with the originating cycle's event log entries."""


async def consume(
    messages: AsyncIterator[dict[str, object]],
    *,
    event_log: EventLog,
    journal: LiveBrokerJournal | None = None,
    cycle_id_resolver: CycleIdResolver | None = None,
    decision_run_id_resolver: CycleIdResolver | None = None,
) -> None:
    """Drain ``messages`` and emit one ``BROKER_LIVE_*`` event per
    decoded trade-update.

    Resolver wiring (one of two paths is required):

    - Pass ``journal=`` and the resolvers default to
      :meth:`~caqrs.execution.live_broker_journal.LiveBrokerJournal.make_resolvers`.
      The journal also receives :meth:`record_fill` /
      :meth:`record_cancel` calls per event for durable persistence.
    - Pass explicit ``cycle_id_resolver=`` + ``decision_run_id_resolver=``
      for tests that don't need a journal. Both must be non-None.

    Passing ``journal=`` alongside explicit resolvers uses the
    explicit ones (testing escape hatch); the journal still receives
    durability calls.

    Returns when the iterator exhausts (production: never; tests: at
    end of the synthetic stream). Cancelling the surrounding task is
    the operator's shutdown signal; this function does not handle
    reconnection.
    """
    if cycle_id_resolver is None and decision_run_id_resolver is None and journal is not None:
        cycle_id_resolver, decision_run_id_resolver = journal.make_resolvers()
    if cycle_id_resolver is None or decision_run_id_resolver is None:
        msg = (
            "consume() requires either journal= OR both cycle_id_resolver= "
            "and decision_run_id_resolver="
        )
        raise ValueError(msg)
    async for raw in messages:
        try:
            update = decode_trade_update(raw)
        except Exception:
            # Per Codex audit 2026-05-10 blocker 1: a single malformed
            # message MUST NOT tear down the stream task. The decoder
            # is defensive (returns None on most malformed input), but
            # belt-and-suspenders catches let the loop survive any
            # un-anticipated parse failure. Errors here are rare and
            # log-worthy at the operator side; the cycle log itself
            # stays clean.
            continue
        if update is None:
            continue  # silently drop unrelated / malformed events
        cycle_id = cycle_id_resolver(update.client_order_id)
        decision_run_id = decision_run_id_resolver(update.client_order_id)
        if cycle_id is None or decision_run_id is None:
            continue  # the broker didn't submit this in our process
        if update.kind in {TradeUpdateKind.FILL, TradeUpdateKind.PARTIAL_FILL}:
            # Decoder guarantees qty + price are non-None and positive
            # for FILL / PARTIAL_FILL (Codex audit blockers 1 + 2);
            # the assert is for mypy narrowing, not runtime defence.
            assert update.filled_qty is not None
            assert update.filled_avg_price is not None
            # Persist BEFORE emitting the event (Codex PR #100
            # consistency note): journal state is at-least-as-fresh as
            # the in-memory event log. When the journal returns False
            # (duplicate (client_order_id, fill_id) suppressed), skip
            # the event_log.append so downstream consumers see exactly
            # one BROKER_LIVE_FILLED per fill — Codex PR #101 finding 2.
            if journal is not None:
                inserted = journal.record_fill(
                    client_order_id=update.client_order_id,
                    qty=update.filled_qty,
                    fill_price_usd=update.filled_avg_price,
                    is_partial=update.kind is TradeUpdateKind.PARTIAL_FILL,
                    fill_id=update.fill_id,
                )
                if not inserted:
                    continue
            event_log.append(
                broker_live_filled_event(
                    cycle_id=cycle_id,
                    decision_run_id=decision_run_id,
                    order_id=update.order_id,
                    client_order_id=update.client_order_id,
                    symbol=update.symbol,
                    side=update.side,
                    filled_qty=str(update.filled_qty),
                    filled_avg_price_usd=str(update.filled_avg_price),
                    is_partial=update.kind is TradeUpdateKind.PARTIAL_FILL,
                ),
            )
        else:
            # CANCELED or REJECTED
            if journal is not None:
                journal.record_cancel(
                    client_order_id=update.client_order_id,
                    reason=update.reason,
                )
            event_log.append(
                broker_live_cancelled_event(
                    cycle_id=cycle_id,
                    decision_run_id=decision_run_id,
                    order_id=update.order_id,
                    client_order_id=update.client_order_id,
                    symbol=update.symbol,
                    side=update.side,
                    reason=update.reason,
                ),
            )
