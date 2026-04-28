"""In-memory paper broker.

Models the simplest possible broker for the P3.d-1 slice:

- **ADOPT** → for each target, allocate ``capital * weight`` USD of
  notional, fill at the supplied mid price, increment positions.
  All-or-nothing: if **any** target lacks a price, no fills are
  applied and the report is REJECTED.
- **DEFER / REJECT** → no broker work, report is SKIPPED.

Realized-PnL tracking, position-aware rebalance (sell down + buy up),
and partial fills land in P3.d-2.
"""

from __future__ import annotations

from decimal import Decimal

from caqrs.execution.execution_report import (
    ExecutionReport,
    ExecutionStatus,
    Fill,
    FillStatus,
)
from caqrs.policy.gateway import FeasibleAction
from caqrs.schemas.common import Ticker
from caqrs.schemas.decision import DecisionAction


class PaperBroker:
    """:class:`BrokerProtocol` implementation backed by in-memory state.

    Constructed with a positive starting capital. ``positions`` and
    ``capital_usd`` are read-only views of the broker's current state.
    """

    def __init__(self, *, initial_capital_usd: Decimal) -> None:
        if initial_capital_usd <= 0:
            msg = f"initial_capital_usd must be positive; got {initial_capital_usd}"
            raise ValueError(msg)
        self._capital = initial_capital_usd
        self._positions: dict[str, Decimal] = {}

    @property
    def capital_usd(self) -> Decimal:
        return self._capital

    @property
    def positions(self) -> dict[str, Decimal]:
        return dict(self._positions)

    async def execute(
        self,
        *,
        action: FeasibleAction,
        prices: dict[Ticker, Decimal],
    ) -> ExecutionReport:
        if action.action != DecisionAction.ADOPT:
            return ExecutionReport(
                source_decision_run_id=action.source_decision_run_id,
                status=ExecutionStatus.SKIPPED,
                fills=(),
                reason=f"action={action.action.value}; no broker work",
            )

        # All-or-nothing: collect missing prices first.
        missing = [t.ticker for t in action.targets if t.ticker not in prices]
        if missing:
            rejected_fills = tuple(
                Fill(
                    ticker=target.ticker,
                    side=target.side,
                    status=FillStatus.REJECTED,
                    quantity=Decimal(0),
                    fill_price_usd=Decimal(0),
                    notional_usd=Decimal(0),
                    reason=("price unavailable" if target.ticker in missing else "rolled back"),
                )
                for target in action.targets
            )
            return ExecutionReport(
                source_decision_run_id=action.source_decision_run_id,
                status=ExecutionStatus.REJECTED,
                fills=rejected_fills,
                reason=f"missing prices for: {', '.join(missing)}",
            )

        # All targets fillable → emit fills, update state.
        fills: list[Fill] = []
        for target in action.targets:
            price = prices[target.ticker]
            target_notional = self._capital * target.weight
            quantity = target_notional / price if price > 0 else Decimal(0)
            fills.append(
                Fill(
                    ticker=target.ticker,
                    side=target.side,
                    status=FillStatus.FILLED,
                    quantity=quantity,
                    fill_price_usd=price,
                    notional_usd=target_notional,
                    reason=None,
                ),
            )
            self._positions[target.ticker] = (
                self._positions.get(target.ticker, Decimal(0)) + quantity
            )

        return ExecutionReport(
            source_decision_run_id=action.source_decision_run_id,
            status=ExecutionStatus.FILLED,
            fills=tuple(fills),
            reason=None,
        )
