"""LiveBrokerAlpaca — first concrete LiveBroker venue (P4 first-cut).

Per ADR-0009: Alpaca paper trading is the first venue for the live
broker contract proof. This module implements the seven NFRs from
ADR-0008 with **no real Alpaca SDK integration** — the live-submission
code path is short-circuited under the default-off flag and the
kill-switch state, so a venue connection is never opened from this
module.

The follow-up PR adds:

- ``alpaca-py`` as an optional dependency under
  ``[project.optional-dependencies] live-broker``
- the actual REST + websocket client for order submission
- the ``BROKER_LIVE_*`` event log emission via an injected EventLog
- the CLI ``python -m caqrs.execution.live_broker_alpaca confirm-live``
  for the NFR-LIVE-BROKER-1 two-step approval workflow

The current cut is the **safety-contract proof**: the seven NFR
primitives (default-off, credential isolation, dry-run parity,
deterministic idempotency, kill-switch, broker-side loss cap, distinct
event taxonomy) are implementable and verifiable today; the Alpaca
wire format integration follows once this perimeter is reviewed.

Step 1 / Step 2 dispatch (ADR-0006): the methods raise
``NotImplementedError`` in step 1; step 2 fills the bodies and flips
the four currently-deferred xfail markers in
``tests/test_broker_contract.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
from decimal import Decimal
from typing import TextIO

from caqrs.execution.alpaca_rest import AlpacaError, AlpacaRestClient
from caqrs.execution.execution_report import (
    ExecutionReport,
    ExecutionStatus,
    Fill,
    FillStatus,
)
from caqrs.execution.paper_broker import PaperBroker
from caqrs.orchestrator.event_log import EventLog
from caqrs.orchestrator.events import (
    broker_live_kill_switch_event,
    broker_live_rejected_event,
    broker_live_submitted_event,
)
from caqrs.policy.gateway import FeasibleAction
from caqrs.schemas.common import Ticker
from caqrs.schemas.decision import Side

_CLIENT_ORDER_ID_TRUNCATION = 48  # Alpaca-documented client_order_id length cap (ADR-0009)


class LiveBrokerAlpaca:
    """Live-broker adapter for Alpaca paper / live trading.

    Constructed with an injected :class:`PaperBroker` for the NFR-3
    dry-run-parity pre-flight. The default-off flag
    (:attr:`enable_live_orders`) MUST be flipped through the
    NFR-LIVE-BROKER-1 two-step human-approval workflow (env var +
    one-time CLI confirm); the constructor itself does not reach for
    the env var, so unit tests can construct an instance without
    triggering credential reads.

    All public attributes are intentionally direct (not properties)
    so the contract test suite can ``getattr(broker, "...")`` on them
    without triggering side effects. Internal mutable state
    (kill-switch flag, realized-loss accumulator) is consolidated on
    a single ``_state`` dict to keep ownership obvious.
    """

    def __init__(
        self,
        *,
        paper_broker: PaperBroker,
        live_broker_daily_loss_cap_usd: Decimal,
        alpaca_client: AlpacaRestClient | None = None,
        _force_enable_live_orders_for_test: bool = False,
    ) -> None:
        """Construct a LiveBrokerAlpaca in default-off, no-context state.

        ``enable_live_orders`` is **never** a constructor arg — the
        only path that flips it in production is
        :meth:`enable_live_orders_after_human_approval`, which
        enforces the env-var + CLI confirmation workflow per ADR-0008
        §NFR-LIVE-BROKER-1. The underscore-prefixed kwarg
        ``_force_enable_live_orders_for_test`` exists so unit tests can
        exercise the code paths past the default-off short-circuit; it
        MUST NOT be set in production code.

        ``cycle_id`` and ``event_log`` are NOT taken at construction
        time. The runner injects them per-cycle via
        :meth:`attach_cycle_context`; tests call ``attach_cycle_context``
        manually when they want to assert event emission. This makes
        the per-execute context explicit and forbids implicit miswiring
        (Codex audit 2026-05-09 finding 3).
        """
        if live_broker_daily_loss_cap_usd <= 0:
            msg = (
                f"live_broker_daily_loss_cap_usd must be positive; "
                f"got {live_broker_daily_loss_cap_usd}"
            )
            raise ValueError(msg)
        self._paper_broker = paper_broker
        self._alpaca_client = alpaca_client
        self._cycle_id: str | None = None
        self._event_log: EventLog | None = None
        self.enable_live_orders: bool = _force_enable_live_orders_for_test
        self.live_broker_daily_loss_cap_usd: Decimal = live_broker_daily_loss_cap_usd
        self.realized_loss_today_usd: Decimal = Decimal(0)
        self._kill_switch_engaged: bool = False

    def attach_cycle_context(self, *, cycle_id: str, event_log: EventLog) -> None:
        """Attach the per-cycle ``cycle_id`` + ``event_log`` for this
        broker's ``BROKER_LIVE_*`` event emission. The CycleRunner
        calls this before each :meth:`execute`; tests call it manually
        when they want emission turned on.

        Nesting (calling ``attach`` while a context is already live)
        raises ``RuntimeError`` — the per-execute context is
        non-reentrant by design, so two concurrent runners cannot
        silently mis-attribute events to one another's cycles
        (Codex audit 2026-05-09 finding 2).
        """
        if self._cycle_id is not None or self._event_log is not None:
            msg = (
                "LiveBrokerAlpaca.attach_cycle_context called while a "
                "previous context is still attached; the per-execute "
                "lifecycle is non-reentrant. Did a prior cycle skip "
                "detach_cycle_context() (e.g. an exception in execute)?"
            )
            raise RuntimeError(msg)
        self._cycle_id = cycle_id
        self._event_log = event_log

    def detach_cycle_context(self) -> None:
        """Clear the per-cycle context. Always called from the runner's
        ``finally`` so an exception inside :meth:`execute` does not
        leave the broker permanently bound to one cycle's id."""
        self._cycle_id = None
        self._event_log = None

    def enable_live_orders_after_human_approval(
        self,
        *,
        env_token: str = "LIVE_BROKER_ENABLE_LIVE_ORDERS",
        cli_confirmation_token: str,
    ) -> None:
        """Flip :attr:`enable_live_orders` to ``True`` after the
        ADR-0008 §NFR-LIVE-BROKER-1 two-step approval.

        Verification (all three must hold):

        1. ``env_token`` starts with the ``LIVE_BROKER_`` prefix —
           prevents a confused-deputy attack where any known process
           env var (``PATH``, etc.) gets repurposed as the gate
           (Codex audit 2026-05-09 finding 1).
        2. ``os.environ[env_token]`` is set to a non-empty string.
        3. ``cli_confirmation_token`` is **byte-equal** to that env
           value — no whitespace stripping (Codex audit 2026-05-09
           finding 2). ``" secret "`` is not the same secret as
           ``"secret"``; the gate enforces exact equality.

        The two-factor invariant: setting the env var alone is not
        enough (the CLI confirmation has not been performed); running
        the CLI alone is not enough (the env var must be set
        independently). The default ``env_token`` is the canonical
        name from ADR-0008 §NFR-LIVE-BROKER-1.

        Trust model: this is an **operator-friction gate**, not a
        security primitive against a compromised shell. Anyone who can
        read the process env and execute Python in this environment
        can satisfy the gate. The intent is preventing **accidental**
        live enablement (e.g. forgetting `--paper`); per ADR-0008 the
        kill switch + per-broker daily loss cap are the
        defense-in-depth layers against malicious / compromised paths.
        """
        if not env_token.startswith("LIVE_BROKER_"):
            msg = (
                f"env_token must start with 'LIVE_BROKER_' (got {env_token!r}); "
                "the live-broker approval gate is bound to the broker's own "
                "credential surface to prevent confused-deputy reuse of "
                "unrelated env vars"
            )
            raise RuntimeError(msg)
        env_value = os.environ.get(env_token, "")
        if not env_value:
            msg = (
                f"{env_token} env var must be set to a non-empty value before "
                "calling enable_live_orders_after_human_approval; run "
                "`python -m caqrs.execution.live_broker_alpaca confirm-live` first"
            )
            raise RuntimeError(msg)
        if cli_confirmation_token != env_value:
            msg = (
                "cli_confirmation_token does not match the env var; "
                "rerun the confirm-live CLI to refresh the confirmation"
            )
            raise RuntimeError(msg)
        self.enable_live_orders = True

    # --- NFR-LIVE-BROKER-4: idempotency key ---------------------------------

    def compute_idempotency_key(
        self,
        *,
        cycle_id: str,
        decision_run_id: str,
        ticker: Ticker,
        side: Side,
        quantity: Decimal,
    ) -> str:
        """Return the deterministic 64-char sha256 hex digest specified
        by ADR-0008 §NFR-LIVE-BROKER-4.

        Per ADR-0009 §"Per-NFR mapping (NFR-LIVE-BROKER-4)" the venue
        wire format (Alpaca's ``client_order_id``, max 48 chars) is
        derived as the leading 48 chars of this 64-char digest. The
        full digest is persisted alongside the venue-assigned
        ``order_id`` for replay disambiguation.
        """
        # Canonical JSON encoding (sorted keys, no whitespace) so
        # values containing the previous "|" separator cannot collide
        # — Codex audit (2026-05-09) flagged the previous join-based
        # form as ambiguous for the public ``str``-typed signature.
        material = json.dumps(
            {
                "cycle_id": cycle_id,
                "decision_run_id": decision_run_id,
                "ticker": str(ticker),
                "side": side.value,
                "quantity": str(quantity),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    # --- NFR-LIVE-BROKER-5: kill-switch ------------------------------------

    @property
    def kill_switch_engaged(self) -> bool:
        return self._kill_switch_engaged

    def kill_switch(self) -> None:
        """Engage the kill switch.

        Subsequent :meth:`execute` calls MUST return
        ``ExecutionStatus.SKIPPED`` with reason ``"kill switch engaged"``
        until :meth:`reenable_after_human_approval` is called via the
        NFR-LIVE-BROKER-1 workflow.

        Per ADR-0009 §"Per-NFR mapping (NFR-LIVE-BROKER-5)" the
        Alpaca-specific cancel-all + ``trade_suspended_by_user``
        sequence happens in the follow-up PR; the local-state flip is
        sufficient for the safety contract.
        """
        self._kill_switch_engaged = True
        self._emit_kill_switch(reason="manual")

    def reenable_after_human_approval(self) -> None:
        """Disengage the kill switch. MUST be called only after the
        same two-step human approval that flips
        :attr:`enable_live_orders` (per ADR-0008 §NFR-LIVE-BROKER-5)."""
        self._kill_switch_engaged = False

    # --- NFR-LIVE-BROKER-6: broker-side daily loss cap ----------------------

    def record_realized_loss(self, *, amount_usd: Decimal) -> None:
        """Add ``amount_usd`` (positive value = realised loss) to the
        broker's day accumulator; engage kill-switch if the cap is
        breached.

        The accumulator and cap MUST NOT share state with
        :class:`~caqrs.policy.gateway.PolicyGatewayConfig`; the
        duplicate computation is the safety property (defense in
        depth) per ADR-0008.

        Day-boundary semantics: callers reset the accumulator via
        :meth:`reset_day` at the cycle-runner-injected day boundary,
        mirroring ``LossBudgetTracker``'s contract from ADR-0005.

        Codex audit (2026-05-09) flagged that a negative ``amount_usd``
        would let a caller artificially reduce the accumulator and
        unwind the kill-switch trigger; this method now rejects
        negative inputs.
        """
        if amount_usd < 0:
            msg = (
                f"amount_usd must be non-negative (got {amount_usd}); a fill "
                "that nets a gain is not a 'realized loss' for cap-breach purposes"
            )
            raise ValueError(msg)
        self.realized_loss_today_usd += amount_usd
        if (
            self.realized_loss_today_usd > self.live_broker_daily_loss_cap_usd
            and not self._kill_switch_engaged
        ):
            self._kill_switch_engaged = True
            self._emit_kill_switch(reason="cap_breach")

    def reset_day(self) -> None:
        """Reset the day's realised-loss accumulator. The kill switch
        state is **not** reset by this method — re-enable requires the
        explicit human-approval workflow in :meth:`reenable_after_human_approval`."""
        self.realized_loss_today_usd = Decimal(0)

    # --- NFR-LIVE-BROKER-1 + 3: execute (default-off + dry-run parity) -----

    async def execute(
        self,
        *,
        action: FeasibleAction,
        prices: dict[Ticker, Decimal],
    ) -> ExecutionReport:
        """Execute the action through the live venue (Alpaca).

        Three short-circuit paths:

        1. **Kill switch engaged** (NFR-5) → SKIPPED with reason
           ``"kill switch engaged"``.
        2. **Live orders disabled** (NFR-1, default-off) → SKIPPED with
           reason ``"live orders disabled"``.
        3. **Paper pre-flight rejection** (NFR-3 dry-run parity) →
           SKIPPED with reason ``"paper pre-flight rejected: <paper-reason>"``.

        Only when none of the above fire does the live submission path
        attempt to reach Alpaca. That path is a stub in this PR;
        ``NotImplementedError`` is raised so a P4 follow-up cannot
        accidentally elevate to live execution before the Alpaca SDK
        wiring lands.
        """
        if self._kill_switch_engaged:
            self._emit_rejected(
                decision_run_id=action.source_decision_run_id,
                reason="kill switch engaged",
            )
            return ExecutionReport(
                source_decision_run_id=action.source_decision_run_id,
                status=ExecutionStatus.SKIPPED,
                fills=(),
                reason="kill switch engaged",
            )
        if not self.enable_live_orders:
            self._emit_rejected(
                decision_run_id=action.source_decision_run_id,
                reason="live orders disabled",
            )
            return ExecutionReport(
                source_decision_run_id=action.source_decision_run_id,
                status=ExecutionStatus.SKIPPED,
                fills=(),
                reason="live orders disabled",
            )
        # NFR-3 dry-run parity: paper pre-flight before any venue submission.
        paper_report = await self._paper_broker.execute(action=action, prices=prices)
        if paper_report.status is not ExecutionStatus.FILLED:
            paper_reason = paper_report.reason or "no reason given"
            full_reason = f"paper pre-flight {paper_report.status.value}: {paper_reason}"
            self._emit_rejected(
                decision_run_id=action.source_decision_run_id,
                reason=full_reason,
            )
            return ExecutionReport(
                source_decision_run_id=action.source_decision_run_id,
                status=ExecutionStatus.SKIPPED,
                fills=(),
                reason=full_reason,
            )
        # Alpaca submission path — reaching here requires
        # (kill_switch=False AND enable_live_orders=True AND paper
        # pre-flight FILLED), all explicit operator decisions.
        if self._alpaca_client is None:
            msg = (
                "LiveBrokerAlpaca constructed without alpaca_client; cannot "
                "submit live orders. Pass alpaca_client=AlpacaRestClient.from_env() "
                "or its equivalent to enable submission."
            )
            raise RuntimeError(msg)
        return await self._submit_to_alpaca(
            action=action,
            paper_report=paper_report,
            prices=prices,
        )

    async def _submit_to_alpaca(
        self,
        *,
        action: FeasibleAction,
        paper_report: ExecutionReport,
        prices: dict[Ticker, Decimal],
    ) -> ExecutionReport:
        """Submit each FILLED entry in ``paper_report`` to Alpaca via
        :class:`AlpacaRestClient`. Aborts on the first venue rejection
        (returns REJECTED + emits BROKER_LIVE_REJECTED); on success
        returns FILLED with placeholder fill prices and emits
        BROKER_LIVE_SUBMITTED per accepted order.

        ``client_order_id`` is the leading 48 chars of
        :meth:`compute_idempotency_key`'s 64-char digest per ADR-0009;
        the full 64-char key is logged on the BROKER_LIVE_SUBMITTED
        payload so replay disambiguation is recoverable post-hoc.
        """
        assert self._alpaca_client is not None  # narrowed by caller
        live_fills: list[Fill] = []
        for paper_fill in paper_report.fills:
            if paper_fill.status is not FillStatus.FILLED:
                continue
            full_key = self.compute_idempotency_key(
                cycle_id=self._cycle_id or "",
                decision_run_id=action.source_decision_run_id,
                ticker=paper_fill.ticker,
                side=paper_fill.side,
                quantity=paper_fill.quantity,
            )
            client_order_id = full_key[:_CLIENT_ORDER_ID_TRUNCATION]
            try:
                order = await self._alpaca_client.submit_order(
                    symbol=paper_fill.ticker,
                    qty=paper_fill.quantity,
                    side=paper_fill.side,
                    client_order_id=client_order_id,
                )
            except AlpacaError as exc:
                reason = f"Alpaca rejected {paper_fill.ticker} {paper_fill.side.value}: {exc}"
                self._emit_rejected(
                    decision_run_id=action.source_decision_run_id,
                    reason=reason,
                )
                return ExecutionReport(
                    source_decision_run_id=action.source_decision_run_id,
                    status=ExecutionStatus.REJECTED,
                    fills=tuple(live_fills),
                    reason=reason,
                )
            self._emit_submitted(
                decision_run_id=action.source_decision_run_id,
                order_id=order.order_id,
                client_order_id=order.client_order_id,
                idempotency_key=full_key,
                symbol=order.symbol,
                qty=order.qty,
                side=paper_fill.side,
            )
            # Placeholder fill: actual fill price + qty come from the
            # websocket trade-update stream (next slice). For now record
            # the requested qty and the price from the gateway-side
            # snapshot so downstream consumers see something non-zero.
            placeholder_price = prices.get(paper_fill.ticker, Decimal(0))
            live_fills.append(
                Fill(
                    ticker=paper_fill.ticker,
                    side=paper_fill.side,
                    status=FillStatus.FILLED,
                    quantity=order.qty,
                    fill_price_usd=placeholder_price,
                    notional_usd=order.qty * placeholder_price,
                    reason="venue submission accepted; actual fill price "
                    "via BROKER_LIVE_FILLED event (websocket — next slice)",
                ),
            )
        return ExecutionReport(
            source_decision_run_id=action.source_decision_run_id,
            status=ExecutionStatus.FILLED,
            fills=tuple(live_fills),
            reason=None,
        )

    # --- internal: BROKER_LIVE_* event emission -----------------------------

    def _emit_rejected(self, *, decision_run_id: str, reason: str) -> None:
        # Emission is opt-in: only when an EventLog + cycle_id pair has
        # been attached via attach_cycle_context. Outside a cycle (e.g.
        # operator-driven kill_switch from a CLI before a cycle starts)
        # the broker mutates state but does NOT emit — there's nothing
        # to attribute the event to.
        if self._event_log is None or self._cycle_id is None:
            return
        self._event_log.append(
            broker_live_rejected_event(
                cycle_id=self._cycle_id,
                decision_run_id=decision_run_id,
                reason=reason,
            ),
        )

    def _emit_submitted(
        self,
        *,
        decision_run_id: str,
        order_id: str,
        client_order_id: str,
        idempotency_key: str,
        symbol: str,
        qty: Decimal,
        side: Side,
    ) -> None:
        if self._event_log is None or self._cycle_id is None:
            return
        self._event_log.append(
            broker_live_submitted_event(
                cycle_id=self._cycle_id,
                decision_run_id=decision_run_id,
                order_id=order_id,
                client_order_id=client_order_id,
                idempotency_key=idempotency_key,
                symbol=symbol,
                qty=str(qty),
                side=side.value,
            ),
        )

    def _emit_kill_switch(self, *, reason: str) -> None:
        if self._event_log is None or self._cycle_id is None:
            return  # see _emit_rejected — emission is cycle-context-gated
        self._event_log.append(
            broker_live_kill_switch_event(
                cycle_id=self._cycle_id,
                reason=reason,
            ),
        )


# =====================================================================
# CLI entrypoint — `python -m caqrs.execution.live_broker_alpaca confirm-live`
# =====================================================================
#
# ADR-0008 §NFR-LIVE-BROKER-1 mandates "an env var that requires manual
# setting plus a one-time CLI confirmation". This module's CLI dispatch
# is the second half of that pair: the operator sets
# LIVE_BROKER_ENABLE_LIVE_ORDERS to a secret of their choosing
# (the env var SHOULD be encrypted at rest via dotenvx), then runs
# `python -m caqrs.execution.live_broker_alpaca confirm-live` and
# re-types the same secret. Two-factor in spirit: persistent env var
# config + interactive re-confirm.
#
# `_main(argv, stdin, stdout, stderr)` is parameterised so unit tests
# can drive it without monkey-patching sys streams. The
# `if __name__ == "__main__":` guard at the bottom invokes it with the
# real sys streams.


_CONFIRM_LIVE_ENV_VAR = "LIVE_BROKER_ENABLE_LIVE_ORDERS"


def _main(
    argv: list[str],
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Dispatch the live-broker CLI subcommand. Returns the exit code.

    Currently the only supported subcommand is ``confirm-live``.
    """
    if argv != ["confirm-live"]:
        print(
            "usage: python -m caqrs.execution.live_broker_alpaca confirm-live",
            file=stderr,
        )
        return 2
    secret = os.environ.get(_CONFIRM_LIVE_ENV_VAR, "")
    if not secret:
        print(
            f"{_CONFIRM_LIVE_ENV_VAR} is not set; aborting. "
            "Set it (e.g. via dotenvx) to a secret of your choosing, "
            "then re-run this command.",
            file=stderr,
        )
        return 1
    print(
        "Live trading is about to be authorised. This is a step toward "
        "real-money orders being submitted on your behalf.",
        file=stdout,
    )
    print(
        f"Confirm by re-typing the value of ${_CONFIRM_LIVE_ENV_VAR} exactly:",
        file=stdout,
        flush=True,
    )
    # Strip ONLY the trailing newline that readline() appends; do NOT
    # rstrip() — internal whitespace must round-trip exactly per the
    # byte-equality contract (Codex audit finding 2).
    raw = stdin.readline()
    confirmation = raw[:-1] if raw.endswith("\n") else raw
    if confirmation != secret:
        print(
            "confirmation did not match the env var; aborting. No state was changed.",
            file=stderr,
        )
        return 1
    print(
        "OK. You may now call "
        f"enable_live_orders_after_human_approval(env_token={_CONFIRM_LIVE_ENV_VAR!r}, "
        "cli_confirmation_token=<the same secret you just typed>) "
        "on your LiveBrokerAlpaca instance.",
        file=stdout,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via _main tests
    import sys as _sys

    _sys.exit(_main(_sys.argv[1:], stdin=_sys.stdin, stdout=_sys.stdout, stderr=_sys.stderr))
