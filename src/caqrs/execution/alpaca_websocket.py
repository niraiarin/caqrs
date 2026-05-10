"""Live Alpaca trade-update websocket client.

Per ADR-0008 §NFR-LIVE-BROKER-7 + ADR-0009 §"Per-NFR mapping
(NFR-LIVE-BROKER-7)", the live broker emits ``BROKER_LIVE_FILLED`` /
``BROKER_LIVE_CANCELLED`` events when Alpaca's trade-update stream
confirms fills or cancellations. PR #98 wired the
:func:`caqrs.execution.alpaca_stream.consume` consumer over a generic
``AsyncIterator[dict]``; this module is the production-side adapter
that opens a real websocket to Alpaca's endpoint
(``wss://paper-api.alpaca.markets/stream`` for paper or
``wss://api.alpaca.markets/stream`` for live), authenticates,
subscribes to the trade_updates channel, and yields raw JSON
messages compatible with :func:`consume`.

Architecture: the Connect dependency is injected so tests can drive
the auth / subscribe / reconnect protocol against an in-memory fake
without touching the network. Production wiring imports
:mod:`websockets` lazily inside :func:`trade_updates_stream` so
non-live deployments don't pay for the dependency.

ADR-0006 step 1 / step 2 dispatch: bodies raise
``NotImplementedError`` in step 1; step 2 fills them in.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

# Default endpoints per Alpaca's docs; the live URL is opt-in via
# ``base_url=`` and gated separately by the LiveBroker safety
# perimeter (ADR-0008).
ALPACA_PAPER_WSS_URL = "wss://paper-api.alpaca.markets/stream"
ALPACA_LIVE_WSS_URL = "wss://api.alpaca.markets/stream"


class AlpacaWebSocketAuthError(RuntimeError):
    """Raised when Alpaca rejects the auth handshake. Distinct from
    transient connection failures: the reconnect loop MUST NOT
    swallow this — bad credentials are a config bug the operator
    must fix before any retries are useful."""


class WebSocketLike(Protocol):
    """Subset of :class:`websockets.WebSocketClientProtocol` we use."""

    async def send(self, msg: str) -> None: ...

    async def recv(self) -> str: ...

    def __aiter__(self) -> AsyncIterator[str]: ...


ConnectFn = Callable[[str], AbstractAsyncContextManager[WebSocketLike]]
"""Factory for websocket connections. Production wiring uses
:func:`websockets.connect`; tests inject an in-memory fake."""


SleepFn = Callable[[float], Awaitable[None]]
"""Async sleep injection for deterministic backoff testing."""


async def trade_updates_stream(
    *,
    api_key: str,
    api_secret: str,
    base_url: str = ALPACA_PAPER_WSS_URL,
    connect: ConnectFn | None = None,
    initial_backoff_seconds: float = 1.0,
    max_backoff_seconds: float = 60.0,
    sleep: SleepFn | None = None,
) -> AsyncIterator[dict[str, object]]:
    """Yield Alpaca trade-update messages over a long-lived websocket.

    Protocol per Alpaca's docs:

    1. Connect to ``base_url``.
    2. Send ``{"action":"auth","key":...,"secret":...}``; expect
       ``{"stream":"authorization","data":{"status":"authorized"}}``.
       Any other status raises :class:`AlpacaWebSocketAuthError`.
    3. Send ``{"action":"listen","data":{"streams":["trade_updates"]}}``;
       expect ``{"stream":"listening","data":{"streams":[...]}}``.
    4. Yield every subsequent message, JSON-parsed, as ``dict``.

    Reconnect: any exception raised during step 4 (network blip,
    server-side close) drops the websocket, sleeps with exponential
    backoff (initial → 2× → 4× → ... capped at
    ``max_backoff_seconds``), and re-runs steps 1–3. The backoff
    resets to ``initial_backoff_seconds`` after each successful
    handshake so long-lived streams don't accumulate ever-larger
    waits. Auth failures are NOT retried.
    """
    msg = "trade_updates_stream not implemented yet (TDD step 1)"
    raise NotImplementedError(msg)
    if False:  # pragma: no cover — yield to declare the function as an async generator
        yield {}
