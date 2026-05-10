"""Tests for the live Alpaca trade-update websocket client.

Per ADR-0006 step 1 / step 2:

- **Step 1** (this commit): every test exercises the public surface
  of :mod:`caqrs.execution.alpaca_websocket` via xfail markers; the
  module is a stub returning :class:`NotImplementedError`.
- **Step 2** (next commit): bodies implemented, markers removed,
  the auth + subscribe + reconnect protocol is end-to-end exercised
  against an in-memory fake websocket.

Why a fake websocket: the real Alpaca endpoint requires API
credentials and live network. The contract this module owns is the
auth/subscribe protocol exchange + reconnect-with-backoff loop,
which is fully captured by a deterministic in-memory transport.
The actual TCP/TLS plumbing is delegated to the third-party
``websockets`` library (operator concern).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import pytest

from caqrs.execution.alpaca_websocket import (
    AlpacaWebSocketAuthError,
    trade_updates_stream,
)


class _FakeWebSocket:
    """In-memory websocket mock. ``incoming`` is a queue of pre-canned
    messages the server sends; ``sent`` records every send() the
    client made for assertion."""

    def __init__(self, incoming: list[str], *, raise_on_iter: BaseException | None = None) -> None:
        self._incoming = incoming
        self._idx = 0
        self.sent: list[str] = []
        self._raise_on_iter = raise_on_iter

    async def send(self, msg: str) -> None:
        self.sent.append(msg)

    async def recv(self) -> str:
        if self._idx >= len(self._incoming):
            msg = "fake websocket exhausted"
            raise ConnectionError(msg)
        out = self._incoming[self._idx]
        self._idx += 1
        return out

    def __aiter__(self) -> AsyncIterator[str]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[str]:
        if self._raise_on_iter is not None:
            raise self._raise_on_iter
        while self._idx < len(self._incoming):
            yield self._incoming[self._idx]
            self._idx += 1


def _connect_factory(
    entries: list[_FakeWebSocket | BaseException],
) -> Callable[[str], AbstractAsyncContextManager[_FakeWebSocket]]:
    """Returns a connect() callable that hands out queued items in
    order. ``_FakeWebSocket`` entries open as a websocket; exception
    entries are raised at connect-time (testing pre-handshake
    failures). Running out raises AssertionError so test bugs
    surface immediately."""
    iter_entries = iter(entries)

    @asynccontextmanager
    async def _connect(_url: str) -> AsyncIterator[_FakeWebSocket]:
        try:
            entry = next(iter_entries)
        except StopIteration as exc:
            msg = "no more fake websockets queued"
            raise AssertionError(msg) from exc
        if isinstance(entry, BaseException):
            raise entry
        yield entry

    return _connect


def _auth_ok() -> str:
    return json.dumps({"stream": "authorization", "data": {"status": "authorized"}})


def _auth_unauthorized() -> str:
    return json.dumps({"stream": "authorization", "data": {"status": "unauthorized"}})


def _listening_ack() -> str:
    return json.dumps({"stream": "listening", "data": {"streams": ["trade_updates"]}})


def _trade_update(client_order_id: str, qty: str, price: str) -> str:
    return json.dumps(
        {
            "stream": "trade_updates",
            "data": {
                "event": "fill",
                "execution_id": "exec-1",
                "order": {
                    "id": "venue-uuid",
                    "client_order_id": client_order_id,
                    "symbol": "AAPL",
                    "side": "buy",
                },
                "qty": qty,
                "price": price,
            },
        },
    )


@pytest.mark.asyncio
async def test_trade_updates_stream_sends_auth_then_subscribe() -> None:
    """The first two sends MUST be the auth payload (with key+secret)
    and the trade_updates subscription request, in that order."""
    ws = _FakeWebSocket([_auth_ok(), _listening_ack()])
    stream = trade_updates_stream(
        api_key="K",
        api_secret="S",
        connect=_connect_factory([ws]),
        initial_backoff_seconds=0.0,
    )
    # Drain by closing the generator after recv'ing 0 events.
    agen = stream.__aiter__()
    # Drive enough to complete the handshake by manually advancing.
    # No trade events are queued, so the iterator should naturally end.
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(agen.__anext__(), timeout=0.5)

    assert len(ws.sent) == 2
    auth = json.loads(ws.sent[0])
    assert auth == {"action": "auth", "key": "K", "secret": "S"}
    sub = json.loads(ws.sent[1])
    assert sub == {"action": "listen", "data": {"streams": ["trade_updates"]}}


@pytest.mark.asyncio
async def test_trade_updates_stream_yields_trade_update_messages() -> None:
    """After the auth + subscribe handshake, every subsequent message
    MUST be parsed as JSON and yielded to the caller as ``dict``."""
    ws = _FakeWebSocket(
        [
            _auth_ok(),
            _listening_ack(),
            _trade_update("abc123", "10", "180.50"),
            _trade_update("xyz789", "1", "45.00"),
        ],
    )
    received: list[dict[str, object]] = [
        msg
        async for msg in trade_updates_stream(
            api_key="K",
            api_secret="S",
            connect=_connect_factory([ws]),
            initial_backoff_seconds=0.0,
        )
    ]
    assert len(received) == 2
    data0 = received[0]["data"]
    assert isinstance(data0, dict)
    assert data0["event"] == "fill"
    order = data0["order"]
    assert isinstance(order, dict)
    assert order["client_order_id"] == "abc123"


@pytest.mark.asyncio
async def test_trade_updates_stream_raises_on_unauthorized() -> None:
    """Auth failure is a fatal config error, not a transient
    disconnect: the stream MUST raise so the operator surfaces the
    bad credentials immediately. Reconnect-on-disconnect MUST NOT
    swallow auth failures."""
    ws = _FakeWebSocket([_auth_unauthorized()])
    stream = trade_updates_stream(
        api_key="K",
        api_secret="WRONG",
        connect=_connect_factory([ws]),
        initial_backoff_seconds=0.0,
    )

    async def _drain() -> None:
        async for _ in stream:
            break

    with pytest.raises(AlpacaWebSocketAuthError):
        await _drain()


@pytest.mark.asyncio
async def test_trade_updates_stream_reconnects_on_connection_closed() -> None:
    """When the iteration loop raises ConnectionClosed (or any
    network-level error), the stream MUST drop the websocket,
    reconnect, re-auth, re-subscribe, and continue yielding events
    seamlessly. The caller must not see the disconnect."""
    ws_first = _FakeWebSocket(
        [
            _auth_ok(),
            _listening_ack(),
            _trade_update("abc", "1", "100.00"),
        ],
        raise_on_iter=ConnectionError("network blip"),
    )
    ws_second = _FakeWebSocket(
        [
            _auth_ok(),
            _listening_ack(),
            _trade_update("def", "2", "101.00"),
        ],
    )
    received: list[dict[str, object]] = [
        msg
        async for msg in trade_updates_stream(
            api_key="K",
            api_secret="S",
            connect=_connect_factory([ws_first, ws_second]),
            initial_backoff_seconds=0.0,
            max_backoff_seconds=0.0,
        )
    ]
    # The first websocket's _trade_update before the iter-raise is
    # delivered via the recv() handshake path — actually, reading raises
    # ConnectionError immediately on iter, so first ws yields 0 events.
    # Only the second ws's event is delivered.
    assert len(received) == 1
    data = received[0]["data"]
    assert isinstance(data, dict)
    order = data["order"]
    assert isinstance(order, dict)
    assert order["client_order_id"] == "def"


@pytest.mark.asyncio
async def test_trade_updates_stream_backoff_doubles_until_ceiling() -> None:
    """Exponential backoff for consecutive PRE-handshake failures
    (DNS, TCP, TLS): each failure MUST double the wait, capped at
    ``max_backoff_seconds``. Pre-handshake means connect() itself
    raises before we ever exchange auth — handshake-success would
    reset the backoff (covered by the next test)."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    ws_clean = _FakeWebSocket([_auth_ok(), _listening_ack()])
    async for _ in trade_updates_stream(
        api_key="K",
        api_secret="S",
        connect=_connect_factory(
            [
                ConnectionError("dns blip 1"),
                ConnectionError("dns blip 2"),
                ConnectionError("dns blip 3"),
                ws_clean,
            ],
        ),
        initial_backoff_seconds=1.0,
        max_backoff_seconds=4.0,
        sleep=fake_sleep,
    ):
        pass
    # 1.0 → 2.0 → 4.0 (capped); fourth connect succeeds → no further sleeps.
    assert sleeps == [1.0, 2.0, 4.0]


# === Codex PR #105 round 1 regressions ================================


@pytest.mark.asyncio
async def test_trade_updates_stream_drops_malformed_json_and_continues() -> None:
    """Codex PR #105 minor 2: a single malformed JSON frame in the
    middle of a healthy stream MUST NOT trigger reconnect or stop
    iteration — drop and continue, mirroring
    alpaca_stream.consume()'s defensive posture for downstream
    parse failures."""
    ws = _FakeWebSocket(
        [
            _auth_ok(),
            _listening_ack(),
            _trade_update("first", "1", "100.00"),
            "{not valid json",  # malformed
            _trade_update("second", "2", "101.00"),
        ],
    )
    received: list[dict[str, object]] = [
        msg
        async for msg in trade_updates_stream(
            api_key="K",
            api_secret="S",
            connect=_connect_factory([ws]),
            initial_backoff_seconds=0.0,
        )
    ]
    assert len(received) == 2
    data_first = received[0]["data"]
    assert isinstance(data_first, dict)
    order_first = data_first["order"]
    assert isinstance(order_first, dict)
    assert order_first["client_order_id"] == "first"
    data_second = received[1]["data"]
    assert isinstance(data_second, dict)
    order_second = data_second["order"]
    assert isinstance(order_second, dict)
    assert order_second["client_order_id"] == "second"


@pytest.mark.asyncio
async def test_trade_updates_stream_propagates_fatal_exceptions() -> None:
    """Codex PR #105 round 1 major: a fatal programming/config error
    (e.g. TypeError from a bad ws stub) MUST propagate out instead
    of triggering an infinite reconnect loop. Only network-transport
    errors are retryable."""
    ws_bad = _FakeWebSocket(
        [_auth_ok(), _listening_ack()],
        raise_on_iter=TypeError("programmer error"),
    )
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with pytest.raises(TypeError, match="programmer error"):
        async for _ in trade_updates_stream(
            api_key="K",
            api_secret="S",
            connect=_connect_factory([ws_bad]),
            initial_backoff_seconds=1.0,
            sleep=fake_sleep,
        ):
            pass
    # Fatal — no sleep, no retry attempted.
    assert sleeps == []


@pytest.mark.asyncio
async def test_trade_updates_stream_propagates_cancelled_error() -> None:
    """Codex PR #105 minor 1: ``asyncio.CancelledError`` MUST
    propagate out so task cancellation works correctly. The catch-all
    must not swallow it (and ``CancelledError`` is a ``BaseException``
    subclass, so ``except Exception`` correctly does not catch it —
    this test is a regression guard against future edits that might
    widen to ``BaseException``)."""
    ws_cancelled = _FakeWebSocket(
        [_auth_ok(), _listening_ack()],
        raise_on_iter=asyncio.CancelledError(),
    )

    with pytest.raises(asyncio.CancelledError):
        async for _ in trade_updates_stream(
            api_key="K",
            api_secret="S",
            connect=_connect_factory([ws_cancelled]),
            initial_backoff_seconds=0.0,
        ):
            pass


@pytest.mark.asyncio
async def test_trade_updates_stream_resets_backoff_on_successful_auth() -> None:
    """A successful auth+subscribe handshake means the connection is
    healthy; the subsequent disconnect-after-handshake MUST reset
    the backoff to its initial value, otherwise long-lived streams
    accumulate ever-larger sleeps after each transient blip."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    # Three blips that each get past handshake then disconnect; one
    # final clean connection that yields nothing.
    def ws_blip() -> _FakeWebSocket:
        return _FakeWebSocket(
            [_auth_ok(), _listening_ack()],
            raise_on_iter=ConnectionError("post-handshake blip"),
        )

    ws_final = _FakeWebSocket([_auth_ok(), _listening_ack()])
    async for _ in trade_updates_stream(
        api_key="K",
        api_secret="S",
        connect=_connect_factory([ws_blip(), ws_blip(), ws_blip(), ws_final]),
        initial_backoff_seconds=1.0,
        max_backoff_seconds=4.0,
        sleep=fake_sleep,
    ):
        pass
    # After each successful handshake, the next disconnect's wait
    # MUST be the initial value, not the doubled previous value.
    assert sleeps == [1.0, 1.0, 1.0]
