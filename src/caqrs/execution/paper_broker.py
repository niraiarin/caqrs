"""In-memory paper broker.

Models a long-only single-account broker that consumes
:class:`FeasibleAction` decisions and produces :class:`ExecutionReport`
records describing what would have happened on a real venue.

Behaviour:

- **ADOPT** → full **rebalance** to the supplied target weights against
  current total equity (cash + mark-to-market positions). The broker
  emits SELL fills first (closing or trimming over-allocations) and
  BUY fills second (filling under-allocations). On each SELL,
  ``realized_pnl_usd`` accrues ``(sale_price - avg_cost) * qty_sold``;
  BUYs update the holding's weighted-average cost.
- **DEFER / REJECT** → no broker work, ``ExecutionStatus.SKIPPED``.

All-or-nothing semantics (mirrors the gateway's demote-whole): if any
ticker — new target **or** existing position needing mark-to-market —
lacks a price in the supplied snapshot, the whole report is REJECTED
and broker state is unchanged.

Realized-PnL aggregation per-day, per-month, etc. is the **caller's**
responsibility. The broker exposes the cumulative figure since
construction; a future ``LossBudgetTracker`` is expected to compute the
day's delta and feed it into
``PolicyGatewayConfig.daily_realized_loss_usd``.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field

from caqrs.execution.execution_report import (
    ExecutionReport,
    ExecutionStatus,
    Fill,
    FillStatus,
)
from caqrs.policy.gateway import FeasibleAction
from caqrs.schemas.common import StrictBaseModel, Ticker
from caqrs.schemas.decision import DecisionAction, Side


class Position(StrictBaseModel):
    """One holding's quantity + weighted-average entry cost.

    ``avg_cost_usd`` is updated on every additional BUY (weighted across
    cumulative quantity); SELLs leave ``avg_cost_usd`` unchanged because
    a sale crystallises a realised gain/loss against that cost basis
    rather than re-pricing the remaining shares.
    """

    qty: Decimal = Field(ge=0)
    avg_cost_usd: Decimal = Field(ge=0)


class PaperBroker:
    """:class:`BrokerProtocol` implementation backed by in-memory state.

    Constructed with a positive starting cash balance.
    ``cash_usd`` / ``positions`` / ``realized_pnl_usd`` are read-only
    views of the broker's current state.
    """

    def __init__(self, *, initial_capital_usd: Decimal) -> None:
        if initial_capital_usd <= 0:
            msg = f"initial_capital_usd must be positive; got {initial_capital_usd}"
            raise ValueError(msg)
        self._cash = initial_capital_usd
        self._positions: dict[str, Position] = {}
        self._realized_pnl_usd = Decimal(0)

    @property
    def cash_usd(self) -> Decimal:
        return self._cash

    @property
    def positions(self) -> dict[str, Position]:
        return dict(self._positions)

    @property
    def realized_pnl_usd(self) -> Decimal:
        return self._realized_pnl_usd

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

        # All-or-nothing: every involved ticker (new target OR existing
        # position needing MtM) must have a price.
        target_tickers = {t.ticker for t in action.targets}
        involved_tickers = target_tickers | set(self._positions)
        missing = sorted(t for t in involved_tickers if t not in prices)
        if missing:
            return self._reject_report(
                action=action,
                involved_tickers=sorted(involved_tickers),
                missing=missing,
            )

        # Compute total equity at supplied mid prices.
        equity = self._cash + sum(
            (pos.qty * prices[ticker] for ticker, pos in self._positions.items()),
            start=Decimal(0),
        )

        # Compute target qty per ticker (0 for tickers being dropped).
        target_qty: dict[str, Decimal] = {ticker: Decimal(0) for ticker in self._positions}
        for target in action.targets:
            price = prices[target.ticker]
            target_qty[target.ticker] = (
                (equity * target.weight) / price if price > 0 else Decimal(0)
            )

        # Order of operations: SELL all over-allocations first (frees
        # cash), then BUY under-allocations.
        sells, buys = self._compute_deltas(target_qty=target_qty, prices=prices)

        fills: list[Fill] = []
        for ticker, sell_qty, sell_price in sells:
            self._apply_sell(ticker=ticker, qty=sell_qty, price=sell_price)
            fills.append(
                Fill(
                    ticker=ticker,
                    side=Side.SELL,
                    status=FillStatus.FILLED,
                    quantity=sell_qty,
                    fill_price_usd=sell_price,
                    notional_usd=sell_qty * sell_price,
                    reason=None,
                ),
            )
        for ticker, buy_qty, buy_price in buys:
            self._apply_buy(ticker=ticker, qty=buy_qty, price=buy_price)
            fills.append(
                Fill(
                    ticker=ticker,
                    side=Side.BUY,
                    status=FillStatus.FILLED,
                    quantity=buy_qty,
                    fill_price_usd=buy_price,
                    notional_usd=buy_qty * buy_price,
                    reason=None,
                ),
            )

        return ExecutionReport(
            source_decision_run_id=action.source_decision_run_id,
            status=ExecutionStatus.FILLED,
            fills=tuple(fills),
            reason=None,
        )

    # === Helpers ===

    def _reject_report(
        self,
        *,
        action: FeasibleAction,
        involved_tickers: list[str],
        missing: list[str],
    ) -> ExecutionReport:
        rejected_fills = tuple(
            Fill(
                ticker=ticker,
                side=Side.BUY,  # opaque on reject — Side is for FILLED fills
                status=FillStatus.REJECTED,
                quantity=Decimal(0),
                fill_price_usd=Decimal(0),
                notional_usd=Decimal(0),
                reason=("price unavailable" if ticker in missing else "rolled back"),
            )
            for ticker in involved_tickers
        )
        return ExecutionReport(
            source_decision_run_id=action.source_decision_run_id,
            status=ExecutionStatus.REJECTED,
            fills=rejected_fills,
            reason=f"missing prices for: {', '.join(missing)}",
        )

    def _compute_deltas(
        self,
        *,
        target_qty: dict[str, Decimal],
        prices: dict[Ticker, Decimal],
    ) -> tuple[
        list[tuple[str, Decimal, Decimal]],
        list[tuple[str, Decimal, Decimal]],
    ]:
        """Return ``(sells, buys)`` where each entry is
        ``(ticker, qty, price)`` and ``qty > 0``."""
        sells: list[tuple[str, Decimal, Decimal]] = []
        buys: list[tuple[str, Decimal, Decimal]] = []
        for ticker, target in target_qty.items():
            current = self._positions.get(
                ticker, Position(qty=Decimal(0), avg_cost_usd=Decimal(0))
            ).qty
            delta = target - current
            if delta > 0:
                buys.append((ticker, delta, prices[ticker]))
            elif delta < 0:
                sells.append((ticker, -delta, prices[ticker]))
        # Stable order across runs: sort by ticker so the cycle log
        # reads deterministically for the same (state, prices) pair.
        sells.sort(key=lambda triple: triple[0])
        buys.sort(key=lambda triple: triple[0])
        return sells, buys

    def _apply_sell(self, *, ticker: str, qty: Decimal, price: Decimal) -> None:
        pos = self._positions[ticker]
        new_qty = pos.qty - qty
        self._realized_pnl_usd += (price - pos.avg_cost_usd) * qty
        self._cash += qty * price
        if new_qty == 0:
            del self._positions[ticker]
        else:
            self._positions[ticker] = Position(qty=new_qty, avg_cost_usd=pos.avg_cost_usd)

    def _apply_buy(self, *, ticker: str, qty: Decimal, price: Decimal) -> None:
        cost = qty * price
        existing = self._positions.get(ticker)
        if existing is None:
            new_pos = Position(qty=qty, avg_cost_usd=price)
        else:
            total_cost = existing.qty * existing.avg_cost_usd + cost
            new_qty = existing.qty + qty
            new_pos = Position(
                qty=new_qty,
                avg_cost_usd=total_cost / new_qty if new_qty > 0 else Decimal(0),
            )
        self._positions[ticker] = new_pos
        self._cash -= cost
