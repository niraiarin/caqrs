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

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from json import JSONDecodeError
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


# Transport-level errors that are always considered transient and
# should trigger a reconnect with backoff. ``OSError`` covers DNS
# resolution failures, refused connections, broken pipes; its
# subclass ``ConnectionError`` makes that explicit. ``TimeoutError``
# is the asyncio I/O timeout. ``EOFError`` is the websockets library's
# raw-frame EOF signal. Per Codex PR #105 round 1: catching bare
# ``Exception`` was too broad — fatal config / programming bugs were
# silently retried forever instead of crashing loud.
_RETRYABLE_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    OSError,
    TimeoutError,
    EOFError,
)


def _is_websockets_transient(exc: BaseException) -> bool:
    """Detect retryable errors from the :mod:`websockets` library
    without requiring it to be installed (live-broker is an optional
    extra). Per the websockets docs, ``ConnectionClosed`` is always
    transient (server / network closed the channel); ``InvalidStatus``
    distinguishes 5xx (transient) from 4xx (fatal config). Any other
    websockets exception (``InvalidURI``, ``InvalidHandshake``, etc.)
    is treated as fatal so the operator sees the misconfiguration.
    """
    try:
        from websockets.exceptions import (  # noqa: PLC0415 — late import for optional dep
            ConnectionClosed,
            InvalidStatus,
        )
    except ImportError:
        return False
    if isinstance(exc, ConnectionClosed):
        return True
    if isinstance(exc, InvalidStatus):
        # 5xx → transient; 4xx → fatal config error.
        status_code = getattr(exc, "status_code", 0)
        return 500 <= status_code < 600  # noqa: PLR2004
    return False


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
    backoff (initial → 2x → 4x → ... capped at
    ``max_backoff_seconds``), and re-runs steps 1-3. The backoff
    resets to ``initial_backoff_seconds`` after each successful
    handshake so long-lived streams don't accumulate ever-larger
    waits. Auth failures are NOT retried.

    Termination: production usage runs forever (the websocket only
    closes if the operator cancels the surrounding task). For tests,
    a graceful end-of-stream from the iterator (no exception) breaks
    the reconnect loop and returns — this is how unit tests with a
    finite-message fake websocket can drain the generator.
    """
    if connect is None:
        connect = _default_connect
    if sleep is None:
        sleep = asyncio.sleep
    backoff = initial_backoff_seconds
    while True:
        try:
            async with connect(base_url) as ws:
                await ws.send(
                    json.dumps({"action": "auth", "key": api_key, "secret": api_secret}),
                )
                auth_resp_raw = await ws.recv()
                auth_resp = json.loads(auth_resp_raw)
                if not _auth_authorized(auth_resp):
                    auth_msg = f"Alpaca rejected auth handshake: {auth_resp_raw}"
                    raise AlpacaWebSocketAuthError(auth_msg)
                await ws.send(
                    json.dumps(
                        {"action": "listen", "data": {"streams": ["trade_updates"]}},
                    ),
                )
                # Drain the listening ack; we don't strictly verify
                # its shape because Alpaca's exact response format
                # has historically drifted (status=success vs streams
                # echo) and either is benign — the only thing we
                # really need is that the server is now in
                # subscribed state, which we assume on receipt.
                _ = await ws.recv()
                # Successful handshake — reset backoff so transient
                # post-handshake disconnects don't accumulate ever-
                # larger waits over the lifetime of the stream.
                backoff = initial_backoff_seconds
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except JSONDecodeError:
                        # A single malformed frame must not tear down
                        # the stream (Codex PR #105 minor 2). Mirrors
                        # alpaca_stream.consume()'s defensive posture
                        # for malformed downstream events.
                        continue
                    yield msg
                # Iterator ended cleanly (production: never; tests:
                # the fake websocket exhausted). Treat as graceful
                # termination so callers' `async for` returns.
                return
        except AlpacaWebSocketAuthError:
            raise
        except _RETRYABLE_TRANSPORT_ERRORS:
            await sleep(backoff)
            backoff = min(backoff * 2, max_backoff_seconds)
            continue
        except Exception as exc:
            if _is_websockets_transient(exc):
                await sleep(backoff)
                backoff = min(backoff * 2, max_backoff_seconds)
                continue
            # Fatal: programming bug, bad URL, 4xx handshake, etc.
            # Re-raise so the operator sees the failure instead of
            # silently spinning in a reconnect loop (Codex PR #105
            # round 1 major).
            raise


def _auth_authorized(resp: object) -> bool:
    """``True`` iff the response carries
    ``data.status == "authorized"``. Defensive against missing /
    wrong-typed fields."""
    if not isinstance(resp, dict):
        return False
    data = resp.get("data")
    if not isinstance(data, dict):
        return False
    return data.get("status") == "authorized"


@AbstractAsyncContextManager.register  # marker only; real impl is the contextlib helper below
class _NotUsed:
    """Placeholder so the decorator-based registration above type-checks."""


def _default_connect(url: str) -> AbstractAsyncContextManager[WebSocketLike]:
    """Production connect: defers to the :mod:`websockets` library.

    Lazy-imported so non-live deployments don't pull in the
    websockets dependency. Operators activate this path by NOT
    passing a ``connect=`` argument to :func:`trade_updates_stream`.
    """
    import websockets  # noqa: PLC0415 — late import for optional dep

    return websockets.connect(url)  # type: ignore[return-value]
