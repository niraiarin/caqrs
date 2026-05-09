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

from decimal import Decimal

from caqrs.execution.execution_report import (
    ExecutionReport,
)
from caqrs.execution.paper_broker import PaperBroker
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
        enable_live_orders: bool = False,
    ) -> None:
        if live_broker_daily_loss_cap_usd <= 0:
            msg = (
                f"live_broker_daily_loss_cap_usd must be positive; "
                f"got {live_broker_daily_loss_cap_usd}"
            )
            raise ValueError(msg)
        self._paper_broker = paper_broker
        self.enable_live_orders: bool = enable_live_orders
        self.live_broker_daily_loss_cap_usd: Decimal = live_broker_daily_loss_cap_usd
        self.realized_loss_today_usd: Decimal = Decimal(0)
        self._kill_switch_engaged: bool = False

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
        raise NotImplementedError(
            "P4 step 1 placeholder; sha256 derivation in step 2",
        )

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
        raise NotImplementedError(
            "P4 step 1 placeholder; kill-switch state flip in step 2",
        )

    def reenable_after_human_approval(self) -> None:
        """Disengage the kill switch. MUST be called only after the
        same two-step human approval that flips
        :attr:`enable_live_orders` (per ADR-0008 §NFR-LIVE-BROKER-5)."""
        raise NotImplementedError(
            "P4 step 1 placeholder; re-enable in step 2",
        )

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
        """
        raise NotImplementedError(
            "P4 step 1 placeholder; cap-trigger logic in step 2",
        )

    def reset_day(self) -> None:
        """Reset the day's realised-loss accumulator. The kill switch
        state is **not** reset by this method — re-enable requires the
        explicit human-approval workflow in :meth:`reenable_after_human_approval`."""
        raise NotImplementedError(
            "P4 step 1 placeholder; day-boundary reset in step 2",
        )

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
        raise NotImplementedError(
            "P4 step 1 placeholder; execute logic in step 2",
        )
