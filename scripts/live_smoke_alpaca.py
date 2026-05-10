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
from urllib.parse import urlparse
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
    # --ticker intentionally NOT exposed: Codex PR #106 review flagged
    # operator-controlled --ticker as a major exposure risk (high-priced
    # tickers like BRK.A would commit far more than the script's
    # nominal cap on a single share). The smoke is locked to AAPL so the
    # exposure is bounded by the AAPL share price (~$150-$250 in 2026).
    # Operators who legitimately need to smoke other tickers should edit
    # this script — the friction is intentional.
    parser.add_argument(
        "--ticker",
        default="AAPL",
        choices=["AAPL"],
        help="Locked to AAPL (Codex PR #106 review). See module docstring.",
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
        "--max-shares",
        type=int,
        default=1,
        help=(
            "Refuse to submit if the paper pre-flight resolves a "
            "qty exceeding this cap. Default 1 — keeps every smoke "
            "run to a single share regardless of --ticker (Codex "
            "PR #106 review: --ticker is operator-controlled, so a "
            "high-priced symbol could otherwise commit more capital "
            "than the script's nominal $250 estimate)."
        ),
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
            "two-step approval. REQUIRED when --live-submit is set "
            "(the env var alone is not sufficient — the CLI flag is "
            "what captures the operator's intent in shell history). "
            "Codex PR #106 review."
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
    """``True`` if the URL is the production Alpaca endpoint.

    Codex PR #106 blocker: substring matching was defeatable by
    URLs where ``paper-api.alpaca.markets`` appeared in userinfo or
    query (e.g. ``https://paper-api.alpaca.markets@api.alpaca.markets``).
    Parse with :func:`urllib.parse.urlparse` and inspect ``hostname``
    only — that's the actual TCP target.
    """
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    if host == "paper-api.alpaca.markets":
        return False
    return host == "api.alpaca.markets"


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
            f"[error] LIVE_BROKER_BASE_URL={base_url!r} resolves to the "
            "production live endpoint, not paper. Refusing to run. "
            "If you genuinely mean live, pass --i-know-this-is-live.",
            file=sys.stderr,
        )
        return 2

    # Codex PR #106 minor 1: --confirm-token is REQUIRED for --live-submit.
    # The env-only fallback is gone — the CLI flag is what captures the
    # operator's intent in shell history.
    if args.live_submit and not args.confirm_token:
        print(
            "[error] --confirm-token is required when --live-submit is set "
            "(must byte-match $LIVE_BROKER_ENABLE_LIVE_ORDERS per "
            "NFR-LIVE-BROKER-1).",
            file=sys.stderr,
        )
        return 2

    args.journal_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"[plan] base_url={base_url}")
    print(f"[plan] wss_url={wss_url}")
    print(f"[plan] journal={args.journal_path}")
    print(f"[plan] mode={'live-submit' if args.live_submit else 'dry-run (auth+subscribe only)'}")
    print(f"[plan] ticker={args.ticker} side={args.side} max_shares={args.max_shares}")

    paper = PaperBroker(initial_capital_usd=Decimal("10000"))
    event_log = EventLog()
    stop_event = asyncio.Event()

    with LiveBrokerJournal(path=args.journal_path) as journal:
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
        # Codex PR #106 major 1: outer try/finally guarantees the
        # websocket task is cleaned up even on unexpected exceptions.
        try:
            # Give the websocket a moment to complete the auth+subscribe
            # handshake. If auth fails, consumer_task surfaces the error.
            await asyncio.sleep(2.0)
            if consumer_task.done() and consumer_task.exception() is not None:
                return 1
            print("[ws] connected; auth+subscribe handshake complete")

            if not args.live_submit:
                print("[dry-run] no order submitted; cleaning up.")
                return 0

            # Codex PR #106 major 2: cap the exposure structurally by
            # checking the paper pre-flight resolves qty <= max_shares.
            # Run a throwaway pre-flight first; if qty exceeds the cap,
            # refuse before broker.execute() has a chance to submit.
            preflight_paper = PaperBroker(initial_capital_usd=Decimal("10000"))
            paper_quote = {Ticker(args.ticker): Decimal("200")}
            preflight_action = FeasibleAction(
                action=DecisionAction.ADOPT,
                targets=(
                    TargetPosition(
                        ticker=Ticker(args.ticker),
                        side=Side(args.side),
                        weight=Decimal("0.02"),
                    ),
                ),
                violations=(),
                source_decision_run_id="smoke-preflight-" + uuid4().hex[:8],
            )
            preflight_report = await preflight_paper.execute(
                action=preflight_action,
                prices=paper_quote,
            )
            preflight_qty = sum((fill.qty for fill in preflight_report.fills), Decimal(0))
            print(
                f"[preflight] paper would submit qty={preflight_qty} "
                f"(cap: {args.max_shares} share(s))",
            )
            if preflight_qty > args.max_shares:
                print(
                    f"[error] paper pre-flight resolves qty={preflight_qty} > "
                    f"--max-shares={args.max_shares}; refusing to submit.",
                    file=sys.stderr,
                )
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
                        cli_confirmation_token=args.confirm_token,
                    )
                except RuntimeError as e:
                    print(
                        f"[error] enable_live_orders_after_human_approval rejected: {e}",
                        file=sys.stderr,
                    )
                    # Codex PR #106 minor 1: when broker rejects, the
                    # likely cause is whitespace or wrong-secret mismatch.
                    # Hint the operator without printing the secret.
                    env_token = os.environ.get("LIVE_BROKER_ENABLE_LIVE_ORDERS", "")
                    if env_token and len(env_token) != len(args.confirm_token):
                        print(
                            f"[hint] $LIVE_BROKER_ENABLE_LIVE_ORDERS length "
                            f"({len(env_token)}) != --confirm-token length "
                            f"({len(args.confirm_token)}); check whitespace.",
                            file=sys.stderr,
                        )
                    return 2
                print(
                    "[ok] live orders enabled (NFR-LIVE-BROKER-1 two-step approval passed)",
                )

                cycle_id = "smoke-" + uuid4().hex[:8]
                decision_run_id = "smoke-decision-" + uuid4().hex[:8]
                # Codex PR #106 minor 2: surface attribution fields so the
                # operator can correlate journal rows + event log entries.
                print(f"[submit] cycle_id={cycle_id}")
                print(f"[submit] decision_run_id={decision_run_id}")
                broker.attach_cycle_context(cycle_id=cycle_id, event_log=event_log)
                try:
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

                    if report.status not in {
                        ExecutionStatus.SUBMITTED,
                        ExecutionStatus.FILLED,
                    }:
                        print("[done] no live submission occurred; nothing to await.")
                        return 0

                    print(
                        f"[await] waiting up to {args.max_wait_seconds}s for terminal event...",
                    )
                    outcome = await _wait_for_terminal_event(
                        event_log=event_log,
                        cycle_id=cycle_id,
                        timeout_seconds=args.max_wait_seconds,
                    )
                    print(f"[outcome] {outcome}")
                finally:
                    broker.detach_cycle_context()
        finally:
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
