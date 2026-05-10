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
from decimal import Decimal
from enum import StrEnum

from caqrs.orchestrator.event_log import EventLog


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


def decode_trade_update(raw: dict[str, object]) -> AlpacaTradeUpdate | None:
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
    raise NotImplementedError(
        "Alpaca websocket step 1 placeholder; decode impl in step 2",
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
    cycle_id_resolver: CycleIdResolver,
    decision_run_id_resolver: CycleIdResolver,
) -> None:
    """Drain ``messages`` and emit one ``BROKER_LIVE_*`` event per
    decoded trade-update.

    Returns when the iterator exhausts (production: never; tests: at
    end of the synthetic stream). Cancelling the surrounding task is
    the operator's shutdown signal; this function does not handle
    reconnection.
    """
    raise NotImplementedError(
        "Alpaca websocket step 1 placeholder; consume impl in step 2",
    )
