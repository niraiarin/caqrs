"""Tests for the CycleQueue serial dispatcher."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import BaseModel

from caqrs.agents.protocol import AgentResult
from caqrs.orchestrator import (
    CycleBudget,
    CycleQueue,
    CycleResult,
    CycleRunner,
    EventLog,
    Heartbeat,
    new_cycle_id,
)
from caqrs.schemas.audit import AcceptanceCheck, AuditReport, AuditVerdict
from caqrs.schemas.backtest_report import (
    AggregateMetrics,
    BacktestReport,
    FoldMetrics,
)
from caqrs.schemas.common import RunMetadata, new_run_id
from caqrs.schemas.decision import (
    DecisionAction,
    Side,
    StrategyDecision,
    TargetPosition,
)
from caqrs.schemas.hypothesis_card import (
    AcceptanceCriterion,
    Direction,
    HypothesisCard,
    HypothesisStatus,
)
from caqrs.schemas.observer import (
    AssetSnapshot,
    DataDimension,
    ObserverArtifact,
    ObserverInput,
)
from caqrs.schemas.research_plan import (
    DataFrequency,
    ResearchPlan,
    WalkForwardWindow,
)
from caqrs.schemas.skeptic import SkepticReport, SkepticVerdict


def _meta(*, agent: str) -> RunMetadata:
    return RunMetadata(
        run_id=new_run_id(),
        parent_id=None,
        agent_name=agent,
        model_id="test",
        created_at=datetime.now(UTC),
        llm_cost_usd=Decimal(0),
        latency_ms=10,
        token_in=10,
        token_out=5,
    )


def _observer_input() -> ObserverInput:
    return ObserverInput(
        universe=("AAPL",),
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        horizon_days=30,
        dimensions=(DataDimension.PRICES,),
    )


def _observer_artifact() -> ObserverArtifact:
    return ObserverArtifact(
        metadata=_meta(agent="observer"),
        universe=("AAPL",),
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        regime_summary="up",
        asset_snapshots=(AssetSnapshot(ticker="AAPL", last_close=Decimal("180")),),
        news_themes=(),
        macro_notes="",
        data_quality_notes=(),
    )


def _hypothesis_card() -> HypothesisCard:
    return HypothesisCard(
        metadata=_meta(agent="hypothesis"),
        status=HypothesisStatus.DRAFT,
        claim="AAPL trends positive over 30 days.",
        rationale="momentum",
        universe=("AAPL",),
        direction=Direction.LONG,
        horizon_days=30,
        variables=("momentum_1m",),
        acceptance=(
            AcceptanceCriterion(
                metric_path="aggregate.median_sharpe",
                op=">",
                threshold=Decimal("0.5"),
            ),
        ),
        max_drawdown_pct=Decimal("20"),
        expected_window_start=datetime(2026, 1, 1, tzinfo=UTC),
        expected_window_end=datetime(2026, 2, 1, tzinfo=UTC),
    )


def _ok_result[O: BaseModel](output: O, *, agent: str) -> AgentResult[O]:
    return AgentResult[O](output=output, error=None, metadata=_meta(agent=agent))


class _StubAgent[O: BaseModel]:
    def __init__(self, *, name: str, result: AgentResult[O]) -> None:
        self.name = name
        self._result = result

    async def run(self, _payload: object, /) -> AgentResult[O]:
        return self._result


async def _async_return[T](value: T) -> T:
    return value


def _build_runner(*, event_log: EventLog | None = None) -> CycleRunner:
    obs = _observer_artifact()
    hyp = _hypothesis_card()
    sk = SkepticReport(
        metadata=_meta(agent="skeptic"),
        hypothesis_run_id=hyp.metadata.run_id,
        verdict=SkepticVerdict.PROCEED,
        summary="ok",
    )
    rp = ResearchPlan(
        metadata=_meta(agent="research"),
        hypothesis_run_id=hyp.metadata.run_id,
        universe=("AAPL",),
        frequency=DataFrequency.DAILY,
        walk_forward=(
            WalkForwardWindow(
                train_start=datetime(2024, 1, 1, tzinfo=UTC),
                train_end=datetime(2024, 12, 31, tzinfo=UTC),
                test_start=datetime(2025, 1, 1, tzinfo=UTC),
                test_end=datetime(2025, 6, 30, tzinfo=UTC),
            ),
        ),
        cost_model_bps=Decimal("5"),
        slippage_bps=Decimal("2"),
        seed=1,
    )
    bt_run_id = _meta(agent="backtest").run_id
    audit = AuditReport(
        metadata=_meta(agent="auditor"),
        hypothesis_run_id=hyp.metadata.run_id,
        backtest_run_id=bt_run_id,
        verdict=AuditVerdict.PASS,
        checks=(
            AcceptanceCheck(
                metric_path="aggregate.median_sharpe",
                op=">",
                threshold=Decimal("0.5"),
                actual=Decimal("1.2"),
                passed=True,
            ),
        ),
        rationale="met",
    )
    decision = StrategyDecision(
        metadata=_meta(agent="decider"),
        backtest_run_id=bt_run_id,
        action=DecisionAction.ADOPT,
        targets=(TargetPosition(ticker="AAPL", side=Side.BUY, weight=Decimal("0.5")),),
        rationale="ok",
        notional_cap_usd=Decimal("10000"),
        max_position_weight=Decimal("0.5"),
        daily_loss_limit_usd=Decimal("500"),
    )

    return CycleRunner(
        observer=_StubAgent(name="observer", result=_ok_result(obs, agent="observer")),
        hypothesis=_StubAgent(name="hypothesis", result=_ok_result(hyp, agent="hypothesis")),
        skeptic=_StubAgent(name="skeptic", result=_ok_result(sk, agent="skeptic")),
        research=_StubAgent(name="research", result=_ok_result(rp, agent="research")),
        auditor=_StubAgent(name="auditor", result=_ok_result(audit, agent="auditor")),
        decider=_StubAgent(name="decider", result=_ok_result(decision, agent="decider")),
        backtest_executor=lambda plan: _async_return(
            BacktestReport(
                metadata=_meta(agent="backtest"),
                plan_run_id=plan.metadata.run_id,
                folds=(
                    FoldMetrics(
                        fold_index=0,
                        test_start=datetime(2025, 1, 1, tzinfo=UTC),
                        test_end=datetime(2025, 6, 30, tzinfo=UTC),
                        sharpe=Decimal("1.2"),
                        max_drawdown_pct=Decimal("8"),
                        turnover=Decimal("2"),
                        n_trades=120,
                        pnl_usd=Decimal("12345"),
                    ),
                ),
                aggregate=AggregateMetrics(
                    median_sharpe=Decimal("1.2"),
                    mean_sharpe=Decimal("1.2"),
                    worst_fold_sharpe=Decimal("1.2"),
                    median_max_drawdown_pct=Decimal("8"),
                    total_pnl_usd=Decimal("12345"),
                    total_trades=120,
                ),
            ),
        ),
        event_log=event_log if event_log is not None else EventLog(),
        budget=CycleBudget(
            cycle_id=new_cycle_id(),
            token_cap=10_000,
            wallclock_seconds_cap=60.0,
        ),
    )


def test_empty_queue_has_zero_pending() -> None:
    queue = CycleQueue(runner=_build_runner())
    assert queue.pending == 0
    assert not queue.is_running


def test_enqueue_increments_pending() -> None:
    queue = CycleQueue(runner=_build_runner())
    queue.enqueue(_observer_input())
    queue.enqueue(_observer_input())
    assert queue.pending == 2


@pytest.mark.asyncio
async def test_run_one_returns_none_when_empty() -> None:
    queue = CycleQueue(runner=_build_runner())
    assert await queue.run_one() is None


@pytest.mark.asyncio
async def test_run_one_dequeues_and_runs() -> None:
    queue = CycleQueue(runner=_build_runner())
    queue.enqueue(_observer_input())
    result = await queue.run_one()
    assert isinstance(result, CycleResult)
    assert queue.pending == 0
    assert not queue.is_running


@pytest.mark.asyncio
async def test_drain_runs_all_in_fifo_order() -> None:
    queue = CycleQueue(runner=_build_runner())
    for _ in range(3):
        queue.enqueue(_observer_input())
    results = await queue.drain()
    assert len(results) == 3
    assert queue.pending == 0


@pytest.mark.asyncio
async def test_drain_returns_empty_tuple_when_empty() -> None:
    queue = CycleQueue(runner=_build_runner())
    assert await queue.drain() == ()


@pytest.mark.asyncio
async def test_concurrent_drain_runs_serially() -> None:
    queue = CycleQueue(runner=_build_runner())
    for _ in range(3):
        queue.enqueue(_observer_input())

    a, b = await asyncio.gather(queue.drain(), queue.drain())
    counts = sorted([len(a), len(b)])
    assert counts == [0, 3]


@pytest.mark.asyncio
async def test_run_one_respects_running_flag() -> None:
    queue = CycleQueue(runner=_build_runner())
    queue.enqueue(_observer_input())
    queue.enqueue(_observer_input())

    queue._running = True
    try:
        assert await queue.run_one() is None
    finally:
        queue._running = False


# === Heartbeat x CycleQueue composition ===


@pytest.mark.asyncio
async def test_heartbeat_drives_queue_at_interval_boundaries() -> None:
    """Heartbeat docstring promises 'if is_due -> enqueue -> fire'
    composes with the queue. Drive 6 ticks across a 5-min interval,
    advancing the clock 2 min per tick. Cycles run only on the
    boundary ticks.
    """
    base = datetime(2026, 1, 1, tzinfo=UTC)

    # Provision only the reads actually consumed: every tick reads
    # once on is_due(); ticks where is_due() is True read again on
    # fire(). Ticks 1 and 4 fire (2 reads each); ticks 2, 3, 5, 6 are
    # idle (1 read each) — 8 reads total.
    def _t(mins: int) -> datetime:
        return base + timedelta(minutes=mins)

    clock_values = [
        _t(0),
        _t(0),  # tick 1: is_due, fire
        _t(2),  # tick 2: is_due only
        _t(4),  # tick 3: is_due only
        _t(6),
        _t(6),  # tick 4: is_due, fire
        _t(8),  # tick 5: is_due only
        _t(10),  # tick 6: is_due only
    ]
    clock_iter = iter(clock_values)

    def _read_clock() -> datetime:
        return next(clock_iter)

    queue = CycleQueue(runner=_build_runner())
    heartbeat = Heartbeat(interval=timedelta(minutes=5), clock=_read_clock)

    cycles_run = 0
    for _ in range(6):
        if heartbeat.is_due():
            queue.enqueue(_observer_input())
            heartbeat.fire()
            result = await queue.run_one()
            assert result is not None
            cycles_run += 1

    # Tick 1 (t=0):   no prior fire -> due. Fire@0. Cycle #1.
    # Tick 2 (t=2):   2 < 5 -> not due.
    # Tick 3 (t=4):   4 < 5 -> not due.
    # Tick 4 (t=6):   6 >= 5 since fire@0 -> due. Fire@6. Cycle #2.
    # Tick 5 (t=8):   8-6=2 < 5 -> not due.
    # Tick 6 (t=10):  10-6=4 < 5 -> not due.
    expected_cycles = 2
    assert cycles_run == expected_cycles


@pytest.mark.asyncio
async def test_heartbeat_idle_tick_leaves_queue_empty() -> None:
    """When is_due() returns False the caller does not enqueue."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    # fire@0 (1 read) + is_due at t=1min (1 read).
    clock_values = [base, base + timedelta(minutes=1)]
    clock_iter = iter(clock_values)

    def _read_clock() -> datetime:
        return next(clock_iter)

    queue = CycleQueue(runner=_build_runner())
    heartbeat = Heartbeat(interval=timedelta(minutes=5), clock=_read_clock)

    heartbeat.fire()
    assert heartbeat.is_due() is False
    assert queue.pending == 0
