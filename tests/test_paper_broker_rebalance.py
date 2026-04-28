"""P3.d-2 — Paper broker rebalance + realized PnL.

Extends P3.d-1's open-only model to a full rebalance:

- An ADOPT decision is interpreted as **target weights against total
  equity** (cash + mark-to-market positions). The broker computes
  per-ticker deltas and emits SELL fills for over-allocations + BUY
  fills for under-allocations.
- ``realized_pnl_usd`` accumulates ``(sale_price - avg_cost) * qty_sold``
  on each SELL. Buys update the holding's weighted-average cost.
- All-or-nothing maintained: if any ticker — new target **or** existing
  position needing mark-to-market — lacks a price, the whole report is
  REJECTED and broker state is unchanged.

The "daily" aggregation of realized PnL is the caller's job; the broker
exposes the cumulative figure since construction. A future
``LossBudgetTracker`` reads this and feeds
``PolicyGatewayConfig.daily_realized_loss_usd``.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from caqrs.execution.execution_report import ExecutionStatus, FillStatus
from caqrs.execution.paper_broker import PaperBroker, Position
from caqrs.policy.gateway import FeasibleAction
from caqrs.schemas.common import RunMetadata, new_run_id
from caqrs.schemas.decision import (
    DecisionAction,
    Side,
    StrategyDecision,
    TargetPosition,
)


def _meta() -> RunMetadata:
    return RunMetadata(
        run_id=new_run_id(),
        parent_id=None,
        agent_name="decider",
        model_id="test",
        created_at=datetime.now(UTC),
        llm_cost_usd=Decimal(0),
        latency_ms=0,
        token_in=0,
        token_out=0,
    )


def _adopt(*, targets: tuple[TargetPosition, ...]) -> FeasibleAction:
    decision = StrategyDecision(
        metadata=_meta(),
        backtest_run_id=new_run_id(),
        action=DecisionAction.ADOPT,
        targets=targets,
        rationale="test",
        notional_cap_usd=Decimal("1000000"),
        max_position_weight=Decimal("1"),
        daily_loss_limit_usd=Decimal("100000"),
    )
    return FeasibleAction(
        action=DecisionAction.ADOPT,
        targets=targets,
        violations=(),
        source_decision_run_id=decision.metadata.run_id,
    )


def _t(ticker: str, weight: str) -> TargetPosition:
    return TargetPosition(ticker=ticker, side=Side.BUY, weight=Decimal(weight))


# === Position schema ===


def test_position_is_frozen_extra_forbid() -> None:
    pos = Position(qty=Decimal("100"), avg_cost_usd=Decimal("200"))
    with pytest.raises(ValidationError, match="frozen"):
        pos.qty = Decimal("50")  # type: ignore[misc]
    with pytest.raises(ValidationError):
        Position(  # type: ignore[call-arg]
            qty=Decimal("100"),
            avg_cost_usd=Decimal("200"),
            extra="x",
        )


def test_position_rejects_negative_qty_and_negative_cost() -> None:
    with pytest.raises(ValidationError):
        Position(qty=Decimal("-1"), avg_cost_usd=Decimal("100"))
    with pytest.raises(ValidationError):
        Position(qty=Decimal("1"), avg_cost_usd=Decimal("-1"))


# === Initial state ===


def test_initial_state_zero_realized_pnl_no_positions() -> None:
    broker = PaperBroker(initial_capital_usd=Decimal("100000"))
    assert broker.realized_pnl_usd == Decimal(0)
    assert broker.positions == {}
    assert broker.cash_usd == Decimal("100000")


# === Single ADOPT (open-only path; same behaviour as P3.d-1, new shape) ===


@pytest.mark.asyncio
async def test_first_adopt_opens_positions_at_avg_cost_equal_to_fill() -> None:
    broker = PaperBroker(initial_capital_usd=Decimal("100000"))
    prices = {"AAPL": Decimal("200"), "MSFT": Decimal("400")}
    await broker.execute(
        action=_adopt(targets=(_t("AAPL", "0.5"), _t("MSFT", "0.3"))),
        prices=prices,
    )
    aapl = broker.positions["AAPL"]
    msft = broker.positions["MSFT"]
    assert aapl.qty == Decimal("250")
    assert aapl.avg_cost_usd == Decimal("200")
    assert msft.qty == Decimal("75")
    assert msft.avg_cost_usd == Decimal("400")
    # 50% + 30% allocated; remaining 20% of capital stays as cash.
    assert broker.cash_usd == Decimal("20000")
    assert broker.realized_pnl_usd == Decimal(0)


# === Rebalance: BUY-more updates weighted avg cost ===


@pytest.mark.asyncio
async def test_buy_more_at_lower_price_lowers_weighted_avg_cost() -> None:
    broker = PaperBroker(initial_capital_usd=Decimal("100000"))
    # First buy 250 @ $200 → cash 50000.
    await broker.execute(
        action=_adopt(targets=(_t("AAPL", "0.5"),)),
        prices={"AAPL": Decimal("200")},
    )
    # Price halves; rebalance to 100% AAPL on now-smaller equity.
    # Equity = 50000 cash + 250 * 100 = 75000.
    # Target qty = 75000 * 1.0 / 100 = 750.
    # BUY 500 @ $100 → cash 0.
    await broker.execute(
        action=_adopt(targets=(_t("AAPL", "1.0"),)),
        prices={"AAPL": Decimal("100")},
    )
    aapl = broker.positions["AAPL"]
    assert aapl.qty == Decimal("750")
    # Weighted avg = (250*200 + 500*100) / 750 = 100000 / 750.
    expected_avg = (Decimal("250") * Decimal("200") + Decimal("500") * Decimal("100")) / Decimal(
        "750"
    )
    assert aapl.avg_cost_usd == expected_avg
    # No SELLs, so no realized PnL.
    assert broker.realized_pnl_usd == Decimal(0)
    assert broker.cash_usd == Decimal(0)


# === Rebalance: SELL-down accrues realized PnL ===


@pytest.mark.asyncio
async def test_sell_down_at_profit_accrues_positive_realized_pnl() -> None:
    broker = PaperBroker(initial_capital_usd=Decimal("100000"))
    # Open: 100% AAPL @ $100 → 1000 shares, cash 0, avg=100.
    await broker.execute(
        action=_adopt(targets=(_t("AAPL", "1.0"),)),
        prices={"AAPL": Decimal("100")},
    )
    # Price doubles; rebalance to 50% AAPL.
    # Equity = 0 + 1000*200 = 200000. Target = 100000/200 = 500. Sell 500.
    await broker.execute(
        action=_adopt(targets=(_t("AAPL", "0.5"),)),
        prices={"AAPL": Decimal("200")},
    )
    aapl = broker.positions["AAPL"]
    assert aapl.qty == Decimal("500")
    # Avg cost on the remaining shares unchanged by SELL.
    assert aapl.avg_cost_usd == Decimal("100")
    # Realized PnL = (200 - 100) * 500 = 50000.
    assert broker.realized_pnl_usd == Decimal("50000")
    assert broker.cash_usd == Decimal("100000")


@pytest.mark.asyncio
async def test_sell_down_at_loss_accrues_negative_realized_pnl() -> None:
    broker = PaperBroker(initial_capital_usd=Decimal("100000"))
    # Open: 100% AAPL @ $200 → 500 shares, cash 0, avg=200.
    await broker.execute(
        action=_adopt(targets=(_t("AAPL", "1.0"),)),
        prices={"AAPL": Decimal("200")},
    )
    # Price halves; rebalance to 50% AAPL.
    # Equity = 0 + 500*100 = 50000. Target = 25000/100 = 250. Sell 250.
    await broker.execute(
        action=_adopt(targets=(_t("AAPL", "0.5"),)),
        prices={"AAPL": Decimal("100")},
    )
    # Realized PnL = (100 - 200) * 250 = -25000.
    assert broker.realized_pnl_usd == Decimal("-25000")
    assert broker.cash_usd == Decimal("25000")


# === Rebalance: drop a ticker → close fully ===


@pytest.mark.asyncio
async def test_dropped_ticker_is_fully_closed() -> None:
    broker = PaperBroker(initial_capital_usd=Decimal("100000"))
    # Open: 50/50 AAPL/MSFT @ $200/$400 → AAPL(250,200), MSFT(125,400), cash 0.
    await broker.execute(
        action=_adopt(targets=(_t("AAPL", "0.5"), _t("MSFT", "0.5"))),
        prices={"AAPL": Decimal("200"), "MSFT": Decimal("400")},
    )
    # Rebalance to AAPL only at unchanged prices.
    # MSFT must be marked to market → caller must supply price.
    await broker.execute(
        action=_adopt(targets=(_t("AAPL", "1.0"),)),
        prices={"AAPL": Decimal("200"), "MSFT": Decimal("400")},
    )
    # MSFT closed at $400 (== avg) → realized PnL contribution = 0.
    assert "MSFT" not in broker.positions
    # AAPL: equity = 0 + 250*200 + 125*400 = 100000. Target = 100000/200 = 500.
    # BUY 250 more @ $200 → avg = (250*200 + 250*200)/500 = 200.
    aapl = broker.positions["AAPL"]
    assert aapl.qty == Decimal("500")
    assert aapl.avg_cost_usd == Decimal("200")
    assert broker.realized_pnl_usd == Decimal(0)


# === Fill ordering ===


@pytest.mark.asyncio
async def test_rebalance_emits_sells_then_buys() -> None:
    """SELL fills emitted before BUY fills so the cycle log reads in
    execution order — and so cash is freed up before BUYs that depend
    on it."""
    broker = PaperBroker(initial_capital_usd=Decimal("100000"))
    await broker.execute(
        action=_adopt(targets=(_t("AAPL", "1.0"),)),
        prices={"AAPL": Decimal("100")},
    )
    # Now swap entirely from AAPL to MSFT.
    report = await broker.execute(
        action=_adopt(targets=(_t("MSFT", "1.0"),)),
        prices={"AAPL": Decimal("100"), "MSFT": Decimal("200")},
    )
    assert report.status is ExecutionStatus.FILLED
    statuses = [(f.ticker, f.side) for f in report.fills]
    # First a SELL of AAPL, then a BUY of MSFT.
    assert statuses[0] == ("AAPL", Side.SELL)
    assert statuses[-1] == ("MSFT", Side.BUY)
    assert all(f.status is FillStatus.FILLED for f in report.fills)


# === All-or-nothing on missing MtM price ===


@pytest.mark.asyncio
async def test_missing_price_for_existing_position_rejects_whole_action() -> None:
    """The broker needs a price for every existing position to mark to
    market the equity total — even if that ticker is being closed in
    this rebalance. Missing it means we can't compute the SELL fill
    price, so reject."""
    broker = PaperBroker(initial_capital_usd=Decimal("100000"))
    await broker.execute(
        action=_adopt(targets=(_t("AAPL", "0.5"), _t("MSFT", "0.5"))),
        prices={"AAPL": Decimal("200"), "MSFT": Decimal("400")},
    )
    snapshot_positions = {k: (v.qty, v.avg_cost_usd) for k, v in broker.positions.items()}
    snapshot_cash = broker.cash_usd
    snapshot_pnl = broker.realized_pnl_usd

    # Rebalance to AAPL only but forget MSFT price.
    report = await broker.execute(
        action=_adopt(targets=(_t("AAPL", "1.0"),)),
        prices={"AAPL": Decimal("200")},
    )
    assert report.status is ExecutionStatus.REJECTED
    # Broker state unchanged.
    assert {k: (v.qty, v.avg_cost_usd) for k, v in broker.positions.items()} == snapshot_positions
    assert broker.cash_usd == snapshot_cash
    assert broker.realized_pnl_usd == snapshot_pnl
