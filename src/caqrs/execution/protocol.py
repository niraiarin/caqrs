"""Broker abstraction shared by paper + (future) live brokers.

Structural protocol (PEP 544 ``Protocol``) so concrete implementations
are matched by signature, not by inheritance. Keeps the execution
layer free of broker-class knowledge — the paper broker doesn't depend
on the live broker SDK and vice versa.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from caqrs.execution.execution_report import ExecutionReport
from caqrs.policy.gateway import FeasibleAction
from caqrs.schemas.common import Ticker


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
