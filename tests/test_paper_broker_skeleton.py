"""P3.d-1 — Paper broker skeleton.

The paper broker consumes a :class:`FeasibleAction` and returns a typed
:class:`ExecutionReport` describing what would have happened on a real
venue. No I/O, no order book, no fills outside ``[broker.execute]`` —
this slice models the simplest mid-price full-fill against a
caller-supplied price snapshot.

Design choices for this slice:

- All-or-nothing fills (mirrors Gateway's demote-whole). If any target
  lacks a price, the whole report is ``REJECTED`` and broker state is
  unchanged.
- DEFER / REJECT actions emit ``SKIPPED`` with no broker-state change.
- Position state held in-memory; rebalance / realized-PnL tracking
  arrives in P3.d-2.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from caqrs.execution.execution_report import (
    ExecutionReport,
    ExecutionStatus,
    Fill,
    FillStatus,
)
from caqrs.execution.paper_broker import PaperBroker
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


def _adopt_action(
    *,
    targets: tuple[TargetPosition, ...] = (
        TargetPosition(ticker="AAPL", side=Side.BUY, weight=Decimal("0.5")),
        TargetPosition(ticker="MSFT", side=Side.BUY, weight=Decimal("0.3")),
    ),
) -> FeasibleAction:
    decision = StrategyDecision(
        metadata=_meta(),
        backtest_run_id=new_run_id(),
        action=DecisionAction.ADOPT,
        targets=targets,
        rationale="test",
        notional_cap_usd=Decimal("1000000"),
        max_position_weight=Decimal("0.7"),
        daily_loss_limit_usd=Decimal("10000"),
    )
    return FeasibleAction(
        action=DecisionAction.ADOPT,
        targets=targets,
        violations=(),
        source_decision_run_id=decision.metadata.run_id,
    )


def _defer_action() -> FeasibleAction:
    return FeasibleAction(
        action=DecisionAction.DEFER,
        targets=(),
        violations=(),
        source_decision_run_id=new_run_id(),
    )


# === Schema construction ===


def test_fill_status_values() -> None:
    assert FillStatus.FILLED.value == "filled"
    assert FillStatus.REJECTED.value == "rejected"


def test_execution_status_values() -> None:
    assert ExecutionStatus.FILLED.value == "filled"
    assert ExecutionStatus.SKIPPED.value == "skipped"
    assert ExecutionStatus.REJECTED.value == "rejected"


def test_fill_is_frozen_extra_forbid() -> None:
    fill = Fill(
        ticker="AAPL",
        side=Side.BUY,
        status=FillStatus.FILLED,
        quantity=Decimal("10"),
        fill_price_usd=Decimal("180"),
        notional_usd=Decimal("1800"),
        reason=None,
    )
    with pytest.raises(ValidationError, match="frozen"):
        fill.quantity = Decimal("20")  # type: ignore[misc]
    with pytest.raises(ValidationError):
        Fill(  # type: ignore[call-arg]
            ticker="AAPL",
            side=Side.BUY,
            status=FillStatus.FILLED,
            quantity=Decimal("10"),
            fill_price_usd=Decimal("180"),
            notional_usd=Decimal("1800"),
            reason=None,
            extra="x",
        )


def test_execution_report_carries_source_run_id() -> None:
    rid = new_run_id()
    report = ExecutionReport(
        source_decision_run_id=rid,
        status=ExecutionStatus.SKIPPED,
        fills=(),
        reason="action=defer",
    )
    assert report.source_decision_run_id == rid
    assert report.fills == ()


def test_execution_report_round_trips_through_json() -> None:
    rid = new_run_id()
    report = ExecutionReport(
        source_decision_run_id=rid,
        status=ExecutionStatus.FILLED,
        fills=(
            Fill(
                ticker="AAPL",
                side=Side.BUY,
                status=FillStatus.FILLED,
                quantity=Decimal("10"),
                fill_price_usd=Decimal("180"),
                notional_usd=Decimal("1800"),
                reason=None,
            ),
        ),
        reason=None,
    )
    payload = report.model_dump_json()
    restored = ExecutionReport.model_validate_json(payload)
    assert restored == report


# === Paper broker behaviour: SKIPPED ===


@pytest.mark.asyncio
async def test_defer_action_emits_skipped_no_state_change() -> None:
    broker = PaperBroker(initial_capital_usd=Decimal("100000"))
    initial_positions = dict(broker.positions)
    report = await broker.execute(action=_defer_action(), prices={})
    assert report.status is ExecutionStatus.SKIPPED
    assert report.fills == ()
    assert broker.positions == initial_positions
    assert broker.cash_usd == Decimal("100000")


@pytest.mark.asyncio
async def test_skipped_report_carries_decision_run_id_and_reason() -> None:
    broker = PaperBroker(initial_capital_usd=Decimal("100000"))
    action = _defer_action()
    report = await broker.execute(action=action, prices={})
    assert report.source_decision_run_id == action.source_decision_run_id
    assert "defer" in (report.reason or "")


# === Paper broker behaviour: FILLED ===


@pytest.mark.asyncio
async def test_adopt_action_fills_each_target_at_mid() -> None:
    broker = PaperBroker(initial_capital_usd=Decimal("100000"))
    prices = {"AAPL": Decimal("200"), "MSFT": Decimal("400")}
    action = _adopt_action()
    report = await broker.execute(action=action, prices=prices)

    assert report.status is ExecutionStatus.FILLED
    assert len(report.fills) == 2
    by_ticker = {f.ticker: f for f in report.fills}
    # AAPL: 50% of $100k → $50k → 50000/200 = 250 shares.
    assert by_ticker["AAPL"].quantity == Decimal("250")
    assert by_ticker["AAPL"].fill_price_usd == Decimal("200")
    assert by_ticker["AAPL"].notional_usd == Decimal("50000")
    # MSFT: 30% of $100k → $30k → 30000/400 = 75 shares.
    assert by_ticker["MSFT"].quantity == Decimal("75")
    assert by_ticker["MSFT"].notional_usd == Decimal("30000")


@pytest.mark.asyncio
async def test_filled_report_updates_broker_positions() -> None:
    broker = PaperBroker(initial_capital_usd=Decimal("100000"))
    prices = {"AAPL": Decimal("200"), "MSFT": Decimal("400")}
    await broker.execute(action=_adopt_action(), prices=prices)
    assert broker.positions["AAPL"].qty == Decimal("250")
    assert broker.positions["MSFT"].qty == Decimal("75")


# === Paper broker behaviour: REJECTED (all-or-nothing) ===


@pytest.mark.asyncio
async def test_missing_price_for_any_target_rejects_whole_action() -> None:
    """Mirrors the gateway's demote-whole philosophy: if any leg cannot
    fill, the whole order is rejected and broker state is unchanged."""
    broker = PaperBroker(initial_capital_usd=Decimal("100000"))
    initial_positions = dict(broker.positions)
    # MSFT price omitted.
    prices = {"AAPL": Decimal("200")}
    report = await broker.execute(action=_adopt_action(), prices=prices)

    assert report.status is ExecutionStatus.REJECTED
    assert broker.positions == initial_positions
    # No fills emitted on reject — single-pass all-or-nothing.
    assert all(f.status is FillStatus.REJECTED for f in report.fills)
    rejected_tickers = {f.ticker for f in report.fills if f.status is FillStatus.REJECTED}
    assert "MSFT" in rejected_tickers


@pytest.mark.asyncio
async def test_rejected_report_carries_reason() -> None:
    broker = PaperBroker(initial_capital_usd=Decimal("100000"))
    prices = {"AAPL": Decimal("200")}  # MSFT missing
    report = await broker.execute(action=_adopt_action(), prices=prices)
    assert "MSFT" in (report.reason or "")


# === Capital invariants ===


@pytest.mark.asyncio
async def test_initial_capital_must_be_positive() -> None:
    with pytest.raises(ValueError, match="initial_capital"):
        PaperBroker(initial_capital_usd=Decimal(0))
    with pytest.raises(ValueError, match="initial_capital"):
        PaperBroker(initial_capital_usd=Decimal("-100"))
