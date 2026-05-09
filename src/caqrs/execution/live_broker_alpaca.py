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
from decimal import Decimal

from caqrs.execution.execution_report import (
    ExecutionReport,
    ExecutionStatus,
)
from caqrs.execution.paper_broker import PaperBroker
from caqrs.orchestrator.event_log import EventLog
from caqrs.orchestrator.events import (
    broker_live_kill_switch_event,
    broker_live_rejected_event,
)
from caqrs.policy.gateway import FeasibleAction
from caqrs.schemas.common import Ticker
from caqrs.schemas.decision import Side


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
        cycle_id: str,
        event_log: EventLog | None = None,
        _force_enable_live_orders_for_test: bool = False,
    ) -> None:
        """Construct a LiveBrokerAlpaca in default-off state.

        ``enable_live_orders`` is **never** a constructor arg — the
        only path that flips it in production is
        :meth:`enable_live_orders_after_human_approval`, which
        enforces the env-var + CLI confirmation workflow per ADR-0008
        §NFR-LIVE-BROKER-1. The underscore-prefixed kwarg
        ``_force_enable_live_orders_for_test`` exists so unit tests can
        exercise the code paths past the default-off short-circuit; it
        MUST NOT be set in production code.

        ``cycle_id`` is required so emitted ``BROKER_LIVE_*`` events
        can be correlated with the surrounding cycle.
        ``event_log`` is optional: when provided, this broker emits
        ``BROKER_LIVE_REJECTED`` / ``BROKER_LIVE_KILL_SWITCH`` events
        on every short-circuit / auto-engage path per
        NFR-LIVE-BROKER-7.
        """
        if live_broker_daily_loss_cap_usd <= 0:
            msg = (
                f"live_broker_daily_loss_cap_usd must be positive; "
                f"got {live_broker_daily_loss_cap_usd}"
            )
            raise ValueError(msg)
        self._paper_broker = paper_broker
        self._cycle_id = cycle_id
        self._event_log = event_log
        self.enable_live_orders: bool = _force_enable_live_orders_for_test
        self.live_broker_daily_loss_cap_usd: Decimal = live_broker_daily_loss_cap_usd
        self.realized_loss_today_usd: Decimal = Decimal(0)
        self._kill_switch_engaged: bool = False

    def enable_live_orders_after_human_approval(
        self,
        *,
        env_token: str,
        cli_confirmation_token: str,
    ) -> None:
        """Flip :attr:`enable_live_orders` to ``True`` after the
        ADR-0008 §NFR-LIVE-BROKER-1 two-step approval.

        Stub in this PR: real env-var + CLI-confirmation wiring lands
        with the Alpaca SDK integration in the follow-up. Until then,
        production callers MUST NOT have a path to True; the
        :class:`NotImplementedError` raised here is the binding gate.
        """
        msg = (
            "real env-var + one-time CLI confirmation workflow "
            "deferred to the Alpaca SDK integration follow-up; "
            "the only path to enable_live_orders=True today is the "
            "_force_enable_live_orders_for_test ctor flag, which is "
            "test-only by convention"
        )
        raise NotImplementedError(msg)

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
        # Alpaca submission path — deferred to follow-up PR per ADR-0009.
        # Reaching here requires (kill_switch=False AND enable_live_orders=True
        # AND paper pre-flight FILLED), all explicit operator decisions.
        msg = (
            "live Alpaca submission deferred to follow-up PR; "
            "ADR-0009 §'Implementation checklist' tracks the alpaca-py wiring"
        )
        raise NotImplementedError(msg)

    # --- internal: BROKER_LIVE_* event emission -----------------------------

    def _emit_rejected(self, *, decision_run_id: str, reason: str) -> None:
        if self._event_log is not None:
            self._event_log.append(
                broker_live_rejected_event(
                    cycle_id=self._cycle_id,
                    decision_run_id=decision_run_id,
                    reason=reason,
                ),
            )

    def _emit_kill_switch(self, *, reason: str) -> None:
        if self._event_log is not None:
            self._event_log.append(
                broker_live_kill_switch_event(
                    cycle_id=self._cycle_id,
                    reason=reason,
                ),
            )
