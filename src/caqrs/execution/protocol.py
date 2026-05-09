"""Broker abstraction shared by paper + (future) live brokers.

Structural protocol (PEP 544 ``Protocol``) so concrete implementations
are matched by signature, not by inheritance. Keeps the execution
layer free of broker-class knowledge — the paper broker doesn't depend
on the live broker SDK and vice versa.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from caqrs.execution.execution_report import ExecutionReport
from caqrs.policy.gateway import FeasibleAction
from caqrs.schemas.common import Ticker

if TYPE_CHECKING:
    from caqrs.orchestrator.event_log import EventLog


class BrokerProtocol(Protocol):
    """Async function shape: process a FeasibleAction against a price
    snapshot, return what would have happened.

    Implementations may hold internal state (positions, realized PnL).
    Callers compose: ``gateway → action → broker.execute(action, prices)``.
    """

    async def execute(
        self,
        *,
        action: FeasibleAction,
        prices: dict[Ticker, Decimal],
    ) -> ExecutionReport: ...


@runtime_checkable
class LiveBrokerProtocol(Protocol):
    """Live-broker contract layered on top of :class:`BrokerProtocol`.

    Per ADR-0008 §NFR-LIVE-BROKER-7, live brokers emit their own
    ``BROKER_LIVE_*`` event taxonomy and MUST NOT have ``BROKER_EXECUTED``
    emitted on their behalf by the runner. The runner identifies live
    brokers via :func:`isinstance(broker, LiveBrokerProtocol)` —
    ``runtime_checkable`` so the structural match holds at runtime.

    The protocol bundles three orthogonal duck-typed surfaces a paper
    broker MUST NOT have, so a fresh paper-broker variant can't trip
    the live-broker branch by accident:

    - ``enable_live_orders``: NFR-LIVE-BROKER-1 default-off flag.
    - ``attach_cycle_context`` / ``detach_cycle_context``: lifecycle
      hooks the runner uses to inject the per-cycle ``cycle_id`` and
      shared ``EventLog``. Pairs MUST be matched: each ``attach``
      precedes one ``execute()`` and is followed by ``detach``.
      Nesting (calling ``attach`` while another context is live) MUST
      raise.
    - ``execute``: inherited from :class:`BrokerProtocol`.

    Concurrency note: the attach/detach pair makes the per-execute
    context explicit, so a single live-broker instance shared across
    runners would raise on the second concurrent ``attach`` rather
    than silently mis-attribute events. Callers who need per-cycle
    isolation construct a fresh broker per cycle; callers who reuse
    one broker rely on the attach contract for serialization.
    """

    enable_live_orders: bool

    async def execute(
        self,
        *,
        action: FeasibleAction,
        prices: dict[Ticker, Decimal],
    ) -> ExecutionReport: ...

    def attach_cycle_context(self, *, cycle_id: str, event_log: EventLog) -> None: ...

    def detach_cycle_context(self) -> None: ...
