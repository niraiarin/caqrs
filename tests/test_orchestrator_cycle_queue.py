"""Tests for the CycleQueue serial dispatcher."""

import asyncio
from datetime import UTC, datetime
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
