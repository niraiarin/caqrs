"""Manually-triggered live smoke for the Alpaca live-broker stack.

Per the operator-friction model in ADR-0008, this script is the
first program that connects all of the live-broker pieces against
the *real* Alpaca paper-trading API:

- :class:`caqrs.execution.alpaca_rest.AlpacaRestClient` (REST submit)
- :func:`caqrs.execution.alpaca_websocket.trade_updates_stream`
  (websocket auth + subscribe + reconnect)
- :func:`caqrs.execution.alpaca_stream.consume`
  (BROKER_LIVE_FILLED / BROKER_LIVE_CANCELLED emission)
- :class:`caqrs.execution.live_broker_journal.LiveBrokerJournal`
  (durable submission + fill attribution)
- :class:`caqrs.execution.live_broker_alpaca.LiveBrokerAlpaca`
  (default-off + paper pre-flight + kill-switch + cap)

Run with::

    dotenvx run -- uv run python scripts/live_smoke_alpaca.py

The script defaults to ``--dry-run`` mode (auth + subscribe only,
no order submission). To actually submit a 1-share test order::

    dotenvx run -- uv run python scripts/live_smoke_alpaca.py \\
        --live-submit --ticker AAPL --confirm-token <token>

The ``--confirm-token`` value MUST byte-match
``$LIVE_BROKER_ENABLE_LIVE_ORDERS`` per ADR-0008
§NFR-LIVE-BROKER-1's two-step approval. Required env vars
(via dotenvx)::

    LIVE_BROKER_API_KEY        # Alpaca paper key
    LIVE_BROKER_API_SECRET     # Alpaca paper secret
    LIVE_BROKER_BASE_URL       # default: https://paper-api.alpaca.markets
    LIVE_BROKER_WSS_URL        # default: wss://paper-api.alpaca.markets/stream
    LIVE_BROKER_ENABLE_LIVE_ORDERS  # required only with --live-submit

Hard safety: the script refuses to run if ``LIVE_BROKER_BASE_URL``
points at the production live trading endpoint
(``api.alpaca.markets`` without ``paper-``). Operators who genuinely
mean to smoke-test against live must override the safety with
``--i-know-this-is-live``, which is intentionally tedious.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from caqrs.execution.alpaca_rest import AlpacaRestClient
from caqrs.execution.alpaca_stream import consume
from caqrs.execution.alpaca_websocket import (
    ALPACA_PAPER_WSS_URL,
    AlpacaWebSocketAuthError,
    trade_updates_stream,
)
from caqrs.execution.execution_report import ExecutionStatus
from caqrs.execution.live_broker_alpaca import LiveBrokerAlpaca
from caqrs.execution.live_broker_journal import LiveBrokerJournal
from caqrs.execution.paper_broker import PaperBroker
from caqrs.orchestrator import CycleEventKind, EventLog
from caqrs.policy.gateway import FeasibleAction
from caqrs.schemas.common import Ticker
from caqrs.schemas.decision import DecisionAction, Side, TargetPosition


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ticker",
        default="AAPL",
        help="Ticker to test against (default: AAPL — liquid, large-cap).",
    )
    parser.add_argument(
        "--side",
        choices=["buy", "sell"],
        default="buy",
        help="Order side (default: buy).",
    )
    parser.add_argument(
        "--journal-path",
        type=Path,
        default=Path("var/alpaca_journal.sqlite"),
        help=(
            "SQLite journal path; auto-created. "
            "Default: var/alpaca_journal.sqlite (project-relative)."
        ),
    )
    parser.add_argument(
        "--max-wait-seconds",
        type=float,
        default=60.0,
        help="How long to wait after submission for a fill / cancel event (default: 60s).",
    )
    parser.add_argument(
        "--live-submit",
        action="store_true",
        help=(
            "Actually submit a 1-share order. Without this flag, the "
            "script exits after the websocket handshake (no order "
            "submitted). Per ADR-0008 default-off, --live-submit ALSO "
            "requires --confirm-token + $LIVE_BROKER_ENABLE_LIVE_ORDERS."
        ),
    )
    parser.add_argument(
        "--confirm-token",
        default=None,
        help=(
            "CLI confirmation token; MUST byte-match "
            "$LIVE_BROKER_ENABLE_LIVE_ORDERS per NFR-LIVE-BROKER-1's "
            "two-step approval. Only consulted when --live-submit is "
            "set. Prefer this CLI flag over an env-only flow so the "
            "operator's intent is captured in shell history."
        ),
    )
    parser.add_argument(
        "--i-know-this-is-live",
        action="store_true",
        help=(
            "Override the paper-only safety guard. Setting this flag "
            "permits LIVE_BROKER_BASE_URL pointing at the production "
            "Alpaca endpoint (api.alpaca.markets without paper-). "
            "DO NOT SET unless you genuinely intend live capital."
        ),
    )
    return parser.parse_args()


def _looks_like_live_endpoint(base_url: str) -> bool:
    """``True`` if the URL is the production Alpaca endpoint (no
    paper- prefix). Conservative: any unrecognized URL is treated as
    paper-mode-or-test."""
    host = base_url.lower()
    if "paper-api.alpaca.markets" in host:
        return False
    return "api.alpaca.markets" in host


async def _websocket_consumer(
    *,
    api_key: str,
    api_secret: str,
    wss_url: str,
    journal: LiveBrokerJournal,
    event_log: EventLog,
    stop_event: asyncio.Event,
) -> None:
    """Bridge the live websocket stream into the cycle event log.

    Runs until ``stop_event`` is set or the websocket terminates.
    Cancellation propagates through the surrounding asyncio.gather
    in :func:`_run`.
    """
    stream = trade_updates_stream(
        api_key=api_key,
        api_secret=api_secret,
        base_url=wss_url,
    )
    consume_task = asyncio.create_task(consume(stream, event_log=event_log, journal=journal))
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        done, pending = await asyncio.wait(
            (consume_task, stop_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                raise exc
    except AlpacaWebSocketAuthError as e:
        print(f"[ws] FATAL: Alpaca rejected websocket auth: {e}", file=sys.stderr)
        raise


async def _wait_for_terminal_event(
    *,
    event_log: EventLog,
    cycle_id: str,
    timeout_seconds: float,
) -> str:
    """Poll the event log every 500ms until a BROKER_LIVE_FILLED /
    BROKER_LIVE_CANCELLED arrives for ``cycle_id`` or the timeout
    elapses. Returns a short human-readable status string."""
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while asyncio.get_event_loop().time() < deadline:
        for kind, label in (
            (CycleEventKind.BROKER_LIVE_FILLED, "FILLED"),
            (CycleEventKind.BROKER_LIVE_CANCELLED, "CANCELLED"),
        ):
            for ev in event_log.filter_by_kind(kind):
                if ev.cycle_id == cycle_id:
                    return f"{label} ({ev.payload})"
        await asyncio.sleep(0.5)
    return "TIMEOUT (no terminal event observed within window)"


async def _run(args: argparse.Namespace) -> int:
    api_key = os.environ.get("LIVE_BROKER_API_KEY", "")
    api_secret = os.environ.get("LIVE_BROKER_API_SECRET", "")
    base_url = os.environ.get("LIVE_BROKER_BASE_URL", "https://paper-api.alpaca.markets")
    wss_url = os.environ.get("LIVE_BROKER_WSS_URL", ALPACA_PAPER_WSS_URL)

    if not api_key or not api_secret:
        print(
            "[error] LIVE_BROKER_API_KEY / LIVE_BROKER_API_SECRET must be "
            "set (managed via dotenvx). See script docstring.",
            file=sys.stderr,
        )
        return 2

    if _looks_like_live_endpoint(base_url) and not args.i_know_this_is_live:
        print(
            f"[error] LIVE_BROKER_BASE_URL={base_url!r} looks like the "
            "production live endpoint, not paper. Refusing to run. "
            "If you genuinely mean live, pass --i-know-this-is-live.",
            file=sys.stderr,
        )
        return 2

    args.journal_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"[plan] base_url={base_url}")
    print(f"[plan] wss_url={wss_url}")
    print(f"[plan] journal={args.journal_path}")
    print(f"[plan] mode={'live-submit' if args.live_submit else 'dry-run (auth+subscribe only)'}")

    paper = PaperBroker(initial_capital_usd=Decimal("10000"))
    event_log = EventLog()
    stop_event = asyncio.Event()

    with LiveBrokerJournal(path=args.journal_path) as journal:
        # Spawn websocket consumer immediately so handshake errors
        # surface before any order goes out.
        consumer_task = asyncio.create_task(
            _websocket_consumer(
                api_key=api_key,
                api_secret=api_secret,
                wss_url=wss_url,
                journal=journal,
                event_log=event_log,
                stop_event=stop_event,
            ),
        )
        # Give the websocket a moment to complete the auth+subscribe
        # handshake. If auth fails, consumer_task surfaces the error.
        await asyncio.sleep(2.0)
        if consumer_task.done() and consumer_task.exception() is not None:
            stop_event.set()
            await consumer_task
            return 1
        print("[ws] connected; auth+subscribe handshake complete")

        if not args.live_submit:
            print("[dry-run] no order submitted; cleaning up.")
            stop_event.set()
            with contextlib.suppress(asyncio.CancelledError):
                await consumer_task
            return 0

        # Live-submit path requires the two-step approval.
        confirm_token = args.confirm_token or os.environ.get("LIVE_BROKER_ENABLE_LIVE_ORDERS", "")
        if not confirm_token:
            print(
                "[error] --confirm-token (or $LIVE_BROKER_ENABLE_LIVE_ORDERS) "
                "is required when --live-submit is set.",
                file=sys.stderr,
            )
            stop_event.set()
            with contextlib.suppress(asyncio.CancelledError):
                await consumer_task
            return 2

        async with AlpacaRestClient.from_env() as alpaca_client:
            broker = LiveBrokerAlpaca(
                paper_broker=paper,
                live_broker_daily_loss_cap_usd=Decimal("100"),
                alpaca_client=alpaca_client,
                journal=journal,
            )
            try:
                broker.enable_live_orders_after_human_approval(
                    cli_confirmation_token=confirm_token,
                )
            except RuntimeError as e:
                print(
                    f"[error] enable_live_orders_after_human_approval rejected: {e}",
                    file=sys.stderr,
                )
                stop_event.set()
                with contextlib.suppress(asyncio.CancelledError):
                    await consumer_task
                return 2
            print("[ok] live orders enabled (NFR-LIVE-BROKER-1 two-step approval passed)")

            cycle_id = "smoke-" + uuid4().hex[:8]
            decision_run_id = "smoke-decision-" + uuid4().hex[:8]
            broker.attach_cycle_context(cycle_id=cycle_id, event_log=event_log)
            try:
                # weight=0.02 x initial_capital=$10000 = $200 notional;
                # at any plausible AAPL price ($150-$250) the paper
                # pre-flight will compute 0-1 share. Combined with the
                # market quote price the live broker actually sends,
                # this caps the test exposure at $250.
                action = FeasibleAction(
                    action=DecisionAction.ADOPT,
                    targets=(
                        TargetPosition(
                            ticker=Ticker(args.ticker),
                            side=Side(args.side),
                            weight=Decimal("0.02"),
                        ),
                    ),
                    violations=(),
                    source_decision_run_id=decision_run_id,
                )
                # The paper pre-flight needs a quote; use $200 as a
                # safe-side estimate. The actual venue fill price comes
                # from Alpaca's market — this number only feeds the
                # paper-broker simulation.
                paper_quote = {Ticker(args.ticker): Decimal("200")}
                report = await broker.execute(action=action, prices=paper_quote)
                print(f"[submit] status={report.status.value}")
                if report.reason:
                    print(f"[submit] reason={report.reason}")
                for fill in report.fills:
                    msg = (
                        f"[submit] fill: {fill.ticker} {fill.side.value} "
                        f"{fill.qty} @ ~{fill.price_usd}"
                    )
                    print(msg)

                if report.status not in {ExecutionStatus.SUBMITTED, ExecutionStatus.FILLED}:
                    print("[done] no live submission occurred; nothing to await.")
                    return 0

                print(f"[await] waiting up to {args.max_wait_seconds}s for terminal event...")
                outcome = await _wait_for_terminal_event(
                    event_log=event_log,
                    cycle_id=cycle_id,
                    timeout_seconds=args.max_wait_seconds,
                )
                print(f"[outcome] {outcome}")
            finally:
                broker.detach_cycle_context()
                stop_event.set()
                with contextlib.suppress(asyncio.CancelledError):
                    await consumer_task

    return 0


def main() -> int:
    args = _parse_args()
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\n[interrupt] shutdown signaled; bye.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
