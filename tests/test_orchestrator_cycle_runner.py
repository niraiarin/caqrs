"""Tests for the CycleRunner — the end-to-end research-cycle driver.

The runner stitches the typed agents together with the state machine,
event log, and budget guard. Tests use protocol-matching stub agents so
nothing here exercises the LLM provider stack.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from caqrs.agents.auditor import AuditorInput
from caqrs.agents.decider import DeciderInput
from caqrs.agents.protocol import AgentResult
from caqrs.agents.research import ResearchInput
from caqrs.memory import CycleStore
from caqrs.orchestrator import (
    CycleBudget,
    CycleEventKind,
    CycleResult,
    CycleRunner,
    EventLog,
    OrchestratorState,
    cycle_started_event,
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

# === Fixture builders (artifact factories) ===


def _meta(*, agent: str, token_in: int = 10, token_out: int = 5) -> RunMetadata:
    return RunMetadata(
        run_id=new_run_id(),
        parent_id=None,
        agent_name=agent,
        model_id="test",
        created_at=datetime.now(UTC),
        llm_cost_usd=Decimal(0),
        latency_ms=10,
        token_in=token_in,
        token_out=token_out,
    )


def _observer_input() -> ObserverInput:
    return ObserverInput(
        universe=("AAPL", "MSFT"),
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        horizon_days=30,
        dimensions=(DataDimension.PRICES,),
    )


def _observer_artifact(*, token_in: int = 10, token_out: int = 5) -> ObserverArtifact:
    return ObserverArtifact(
        metadata=_meta(agent="observer", token_in=token_in, token_out=token_out),
        universe=("AAPL", "MSFT"),
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        regime_summary="Trending up.",
        asset_snapshots=(
            AssetSnapshot(ticker="AAPL", last_close=Decimal("180")),
            AssetSnapshot(ticker="MSFT", last_close=Decimal("400")),
        ),
        news_themes=(),
        macro_notes="benign",
        data_quality_notes=(),
    )


def _hypothesis_card(*, token_in: int = 10, token_out: int = 5) -> HypothesisCard:
    return HypothesisCard(
        metadata=_meta(agent="hypothesis", token_in=token_in, token_out=token_out),
        status=HypothesisStatus.DRAFT,
        claim="AAPL outperforms MSFT over the next 30 days.",
        rationale="Recent earnings momentum.",
        universe=("AAPL", "MSFT"),
        direction=Direction.LONG_SHORT,
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


def _skeptic_report(
    *,
    verdict: SkepticVerdict = SkepticVerdict.PROCEED,
    hypothesis_run_id: str,
    token_in: int = 10,
    token_out: int = 5,
) -> SkepticReport:
    if verdict is SkepticVerdict.KILL:
        return SkepticReport(
            metadata=_meta(agent="skeptic", token_in=token_in, token_out=token_out),
            hypothesis_run_id=hypothesis_run_id,
            verdict=verdict,
            falsification_paths=(),
            concerns=("data leakage suspected",),
            summary="Killed.",
        )
    return SkepticReport(
        metadata=_meta(agent="skeptic", token_in=token_in, token_out=token_out),
        hypothesis_run_id=hypothesis_run_id,
        verdict=verdict,
        falsification_paths=(),
        concerns=(),
        summary="Looks fine.",
    )


def _research_plan(
    *, hypothesis_run_id: str, token_in: int = 10, token_out: int = 5
) -> ResearchPlan:
    return ResearchPlan(
        metadata=_meta(agent="research", token_in=token_in, token_out=token_out),
        hypothesis_run_id=hypothesis_run_id,
        universe=("AAPL", "MSFT"),
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
        seed=42,
    )


def _backtest_report(*, plan_run_id: str) -> BacktestReport:
    return BacktestReport(
        metadata=_meta(agent="backtest"),
        plan_run_id=plan_run_id,
        folds=(
            FoldMetrics(
                fold_index=0,
                test_start=datetime(2025, 1, 1, tzinfo=UTC),
                test_end=datetime(2025, 6, 30, tzinfo=UTC),
                sharpe=Decimal("1.2"),
                max_drawdown_pct=Decimal("8"),
                turnover=Decimal("2.0"),
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
    )


def _audit_report(
    *,
    hypothesis_run_id: str,
    backtest_run_id: str,
    verdict: AuditVerdict = AuditVerdict.PASS,
    token_in: int = 10,
    token_out: int = 5,
) -> AuditReport:
    return AuditReport(
        metadata=_meta(agent="auditor", token_in=token_in, token_out=token_out),
        hypothesis_run_id=hypothesis_run_id,
        backtest_run_id=backtest_run_id,
        verdict=verdict,
        checks=(
            AcceptanceCheck(
                metric_path="aggregate.median_sharpe",
                op=">",
                threshold=Decimal("0.5"),
                actual=Decimal("1.2"),
                passed=verdict is AuditVerdict.PASS,
            ),
        ),
        rationale="All criteria met." if verdict is AuditVerdict.PASS else "criteria fail.",
    )


def _strategy_decision(
    *,
    backtest_run_id: str,
    action: DecisionAction = DecisionAction.ADOPT,
    token_in: int = 10,
    token_out: int = 5,
) -> StrategyDecision:
    targets: tuple[TargetPosition, ...] = (
        (TargetPosition(ticker="AAPL", side=Side.BUY, weight=Decimal("0.5")),)
        if action is DecisionAction.ADOPT
        else ()
    )
    return StrategyDecision(
        metadata=_meta(agent="decider", token_in=token_in, token_out=token_out),
        backtest_run_id=backtest_run_id,
        action=action,
        targets=targets,
        rationale="reasonable risk-adjusted return.",
        notional_cap_usd=Decimal("10000"),
        max_position_weight=Decimal("0.5"),
        daily_loss_limit_usd=Decimal("500"),
    )


# === Stub agents (match the structural Agent protocol) ===


class _StubAgent[I: BaseModel, O: BaseModel]:
    def __init__(
        self,
        *,
        name: str,
        result: AgentResult[O],
    ) -> None:
        self.name = name
        self._result = result

    async def run(self, payload: I, /) -> AgentResult[O]:
        return self._result


def _ok_result[O: BaseModel](output: O, *, agent: str) -> AgentResult[O]:
    return AgentResult[O](
        output=output,
        error=None,
        metadata=_meta(
            agent=agent,
            token_in=output.metadata.token_in if hasattr(output, "metadata") else 10,
            token_out=output.metadata.token_out if hasattr(output, "metadata") else 5,
        ),
    )


def _fail_result[O: BaseModel](*, agent: str, error: str) -> AgentResult[O]:
    return AgentResult[O](
        output=None,
        error=error,
        metadata=_meta(agent=agent, token_in=0, token_out=0),
    )


def _build_runner(
    *,
    observer_artifact: ObserverArtifact | None = None,
    hypothesis_card: HypothesisCard | None = None,
    skeptic_verdict: SkepticVerdict = SkepticVerdict.PROCEED,
    research_plan: ResearchPlan | None = None,
    audit_report: AuditReport | None = None,
    decision: StrategyDecision | None = None,
    observer_error: str | None = None,
    backtest_executor: Callable[[ResearchPlan], Awaitable[BacktestReport]] | None = None,
    event_log: EventLog | None = None,
    budget: CycleBudget | None = None,
    clock: Callable[[], datetime] | None = None,
    cycle_store: object = None,  # CycleStoreProtocol; typed loose to avoid cycle in test deps
) -> CycleRunner:
    obs = observer_artifact or _observer_artifact()
    hyp = hypothesis_card or _hypothesis_card()
    sk = _skeptic_report(verdict=skeptic_verdict, hypothesis_run_id=hyp.metadata.run_id)
    rp = research_plan or _research_plan(hypothesis_run_id=hyp.metadata.run_id)

    observer_result: AgentResult[ObserverArtifact] = (
        _fail_result(agent="observer", error=observer_error)
        if observer_error
        else _ok_result(obs, agent="observer")
    )

    bt_executor = backtest_executor or (
        lambda plan: _async_return(_backtest_report(plan_run_id=plan.metadata.run_id))
    )

    backtest_run_id = _meta(agent="backtest").run_id
    audit = audit_report or _audit_report(
        hypothesis_run_id=hyp.metadata.run_id,
        backtest_run_id=backtest_run_id,
    )
    decision_artifact = decision or _strategy_decision(backtest_run_id=backtest_run_id)

    bg = budget or CycleBudget(
        cycle_id=new_cycle_id(),
        token_cap=10_000,
        wallclock_seconds_cap=60.0,
    )
    log = event_log if event_log is not None else EventLog()

    return CycleRunner(
        observer=_StubAgent(name="observer", result=observer_result),
        hypothesis=_StubAgent(name="hypothesis", result=_ok_result(hyp, agent="hypothesis")),
        skeptic=_StubAgent(name="skeptic", result=_ok_result(sk, agent="skeptic")),
        research=_StubAgent(name="research", result=_ok_result(rp, agent="research")),
        auditor=_StubAgent(name="auditor", result=_ok_result(audit, agent="auditor")),
        decider=_StubAgent(
            name="decider",
            result=_ok_result(decision_artifact, agent="decider"),
        ),
        backtest_executor=bt_executor,
        event_log=log,
        budget=bg,
        clock=clock,
        cycle_store=cycle_store,  # type: ignore[arg-type]
    )


async def _async_return[T](value: T) -> T:
    return value


# === Happy path ===


@pytest.mark.asyncio
async def test_runner_completes_full_pipeline() -> None:
    log = EventLog()
    runner = _build_runner(event_log=log)
    result = await runner.run(_observer_input())

    assert isinstance(result, CycleResult)
    assert result.aborted_reason is None
    assert result.terminal_state is OrchestratorState.DECIDING
    assert result.artifacts.observer is not None
    assert result.artifacts.hypothesis is not None
    assert result.artifacts.skeptic is not None
    assert result.artifacts.research is not None
    assert result.artifacts.backtest is not None
    assert result.artifacts.audit is not None
    assert result.artifacts.decision is not None


@pytest.mark.asyncio
async def test_runner_emits_cycle_started_and_completed() -> None:
    log = EventLog()
    runner = _build_runner(event_log=log)
    await runner.run(_observer_input())

    started = log.filter_by_kind(CycleEventKind.CYCLE_STARTED)
    completed = log.filter_by_kind(CycleEventKind.CYCLE_COMPLETED)
    aborted = log.filter_by_kind(CycleEventKind.CYCLE_ABORTED)
    assert len(started) == 1
    assert len(completed) == 1
    assert len(aborted) == 0
    assert completed[0].payload["terminal_state"] == "deciding"


@pytest.mark.asyncio
async def test_runner_emits_one_invoked_per_agent() -> None:
    log = EventLog()
    await _build_runner(event_log=log).run(_observer_input())
    invoked = log.filter_by_kind(CycleEventKind.AGENT_INVOKED)
    assert len(invoked) == 6
    names = [e.payload["agent_name"] for e in invoked]
    assert names == ["observer", "hypothesis", "skeptic", "research", "auditor", "decider"]


@pytest.mark.asyncio
async def test_runner_emits_state_transitions_in_order() -> None:
    log = EventLog()
    await _build_runner(event_log=log).run(_observer_input())
    transitions = log.filter_by_kind(CycleEventKind.STATE_TRANSITION)
    pairs = [(e.payload["src"], e.payload["dst"]) for e in transitions]
    assert pairs == [
        ("idle", "observing"),
        ("observing", "hypothesizing"),
        ("hypothesizing", "scrutinizing"),
        ("scrutinizing", "researching"),
        ("researching", "auditing"),
        ("auditing", "deciding"),
        ("deciding", "idle"),
    ]


@pytest.mark.asyncio
async def test_runner_aggregates_token_usage_in_cycle_completed() -> None:
    log = EventLog()
    await _build_runner(event_log=log).run(_observer_input())
    completed = log.filter_by_kind(CycleEventKind.CYCLE_COMPLETED)[0]
    # 6 agents x (10 in + 5 out) = 90
    assert completed.payload["total_token_in"] == 60
    assert completed.payload["total_token_out"] == 30


# === Skeptic kill (normal early termination) ===


@pytest.mark.asyncio
async def test_runner_short_circuits_on_skeptic_kill() -> None:
    log = EventLog()
    runner = _build_runner(event_log=log, skeptic_verdict=SkepticVerdict.KILL)
    result = await runner.run(_observer_input())
    assert result.aborted_reason is None  # kill is a normal outcome, not an abort
    assert result.terminal_state is OrchestratorState.SCRUTINIZING
    assert result.artifacts.skeptic is not None
    assert result.artifacts.research is None
    assert result.artifacts.backtest is None
    assert result.artifacts.audit is None


@pytest.mark.asyncio
async def test_runner_skeptic_kill_emits_cycle_completed_not_aborted() -> None:
    log = EventLog()
    await _build_runner(event_log=log, skeptic_verdict=SkepticVerdict.KILL).run(
        _observer_input(),
    )
    assert len(log.filter_by_kind(CycleEventKind.CYCLE_COMPLETED)) == 1
    assert len(log.filter_by_kind(CycleEventKind.CYCLE_ABORTED)) == 0


@pytest.mark.asyncio
async def test_runner_skeptic_kill_does_not_invoke_research_or_auditor() -> None:
    log = EventLog()
    await _build_runner(event_log=log, skeptic_verdict=SkepticVerdict.KILL).run(
        _observer_input(),
    )
    invoked_names = [
        e.payload["agent_name"] for e in log.filter_by_kind(CycleEventKind.AGENT_INVOKED)
    ]
    assert "research" not in invoked_names
    assert "auditor" not in invoked_names


# === Agent failure ===


@pytest.mark.asyncio
async def test_runner_aborts_on_observer_failure() -> None:
    log = EventLog()
    runner = _build_runner(event_log=log, observer_error="ProviderError: timeout")
    result = await runner.run(_observer_input())
    assert result.aborted_reason is not None
    assert "timeout" in result.aborted_reason
    assert result.terminal_state is OrchestratorState.ERROR
    assert result.artifacts.observer is None


@pytest.mark.asyncio
async def test_runner_failure_emits_agent_failed_and_cycle_aborted() -> None:
    log = EventLog()
    await _build_runner(event_log=log, observer_error="boom").run(_observer_input())
    failed = log.filter_by_kind(CycleEventKind.AGENT_FAILED)
    aborted = log.filter_by_kind(CycleEventKind.CYCLE_ABORTED)
    assert len(failed) == 1
    assert len(aborted) == 1
    assert "boom" in aborted[0].payload["reason"]


# === Budget enforcement ===


@pytest.mark.asyncio
async def test_runner_aborts_on_token_budget_breach() -> None:
    log = EventLog()
    cycle_id = new_cycle_id()
    # Cap at 10 tokens; observer alone returns 15 (10+5), tripping immediately.
    budget = CycleBudget(
        cycle_id=cycle_id,
        token_cap=10,
        wallclock_seconds_cap=60.0,
    )
    runner = _build_runner(event_log=log, budget=budget)
    result = await runner.run(_observer_input())
    assert result.aborted_reason is not None
    assert "budget" in result.aborted_reason.lower()
    assert len(log.filter_by_kind(CycleEventKind.BUDGET_EXCEEDED)) == 1


@pytest.mark.asyncio
async def test_runner_aborts_on_wallclock_breach() -> None:
    log = EventLog()
    cycle_id = new_cycle_id()
    budget = CycleBudget(
        cycle_id=cycle_id,
        token_cap=10_000,
        wallclock_seconds_cap=1.0,
    )
    base = datetime(2026, 1, 1, tzinfo=UTC)
    # Many calls; advance time fast so the second check trips.
    times = iter(
        [base + timedelta(seconds=i * 0.5) for i in range(50)],
    )
    runner = _build_runner(event_log=log, budget=budget, clock=lambda: next(times))
    result = await runner.run(_observer_input())
    assert result.aborted_reason is not None


# === Result shape ===


@pytest.mark.asyncio
async def test_cycle_result_is_frozen() -> None:
    log = EventLog()
    runner = _build_runner(event_log=log)
    result = await runner.run(_observer_input())

    with pytest.raises(ValidationError, match="frozen"):
        result.cycle_id = "abc"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_runner_supplies_skeptic_with_hypothesis_card() -> None:
    """The skeptic receives the hypothesis card produced upstream, not a stub."""
    captured: list[HypothesisCard] = []

    class _CapturingSkeptic:
        name = "skeptic"

        async def run(self, payload: HypothesisCard, /) -> AgentResult[SkepticReport]:
            captured.append(payload)
            return _ok_result(
                _skeptic_report(
                    verdict=SkepticVerdict.PROCEED,
                    hypothesis_run_id=payload.metadata.run_id,
                ),
                agent="skeptic",
            )

    obs = _observer_artifact()
    hyp = _hypothesis_card()
    research = _research_plan(hypothesis_run_id=hyp.metadata.run_id)
    audit = _audit_report(
        hypothesis_run_id=hyp.metadata.run_id,
        backtest_run_id=_meta(agent="backtest").run_id,
    )

    runner = CycleRunner(
        observer=_StubAgent(name="observer", result=_ok_result(obs, agent="observer")),
        hypothesis=_StubAgent(name="hypothesis", result=_ok_result(hyp, agent="hypothesis")),
        skeptic=_CapturingSkeptic(),
        research=_StubAgent(name="research", result=_ok_result(research, agent="research")),
        auditor=_StubAgent(name="auditor", result=_ok_result(audit, agent="auditor")),
        decider=_StubAgent(
            name="decider",
            result=_ok_result(
                _strategy_decision(backtest_run_id=_meta(agent="backtest").run_id),
                agent="decider",
            ),
        ),
        backtest_executor=lambda plan: _async_return(
            _backtest_report(plan_run_id=plan.metadata.run_id),
        ),
        event_log=EventLog(),
        budget=CycleBudget(
            cycle_id=new_cycle_id(),
            token_cap=10_000,
            wallclock_seconds_cap=60.0,
        ),
    )
    await runner.run(_observer_input())
    assert len(captured) == 1
    assert captured[0].metadata.run_id == hyp.metadata.run_id


@pytest.mark.asyncio
async def test_runner_research_input_carries_skeptic_report() -> None:
    captured: list[ResearchInput] = []

    class _CapturingResearch:
        name = "research"

        async def run(self, payload: ResearchInput, /) -> AgentResult[ResearchPlan]:
            captured.append(payload)
            return _ok_result(
                _research_plan(hypothesis_run_id=payload.hypothesis.metadata.run_id),
                agent="research",
            )

    obs = _observer_artifact()
    hyp = _hypothesis_card()
    sk = _skeptic_report(
        verdict=SkepticVerdict.PROCEED,
        hypothesis_run_id=hyp.metadata.run_id,
    )
    audit = _audit_report(
        hypothesis_run_id=hyp.metadata.run_id,
        backtest_run_id=_meta(agent="backtest").run_id,
    )
    runner = CycleRunner(
        observer=_StubAgent(name="observer", result=_ok_result(obs, agent="observer")),
        hypothesis=_StubAgent(name="hypothesis", result=_ok_result(hyp, agent="hypothesis")),
        skeptic=_StubAgent(name="skeptic", result=_ok_result(sk, agent="skeptic")),
        research=_CapturingResearch(),
        auditor=_StubAgent(name="auditor", result=_ok_result(audit, agent="auditor")),
        decider=_StubAgent(
            name="decider",
            result=_ok_result(
                _strategy_decision(backtest_run_id=_meta(agent="backtest").run_id),
                agent="decider",
            ),
        ),
        backtest_executor=lambda plan: _async_return(
            _backtest_report(plan_run_id=plan.metadata.run_id),
        ),
        event_log=EventLog(),
        budget=CycleBudget(
            cycle_id=new_cycle_id(),
            token_cap=10_000,
            wallclock_seconds_cap=60.0,
        ),
    )
    await runner.run(_observer_input())
    assert len(captured) == 1
    assert captured[0].hypothesis.metadata.run_id == hyp.metadata.run_id
    assert captured[0].skeptic.verdict is SkepticVerdict.PROCEED


@pytest.mark.asyncio
async def test_runner_auditor_input_carries_backtest() -> None:
    captured: list[AuditorInput] = []

    class _CapturingAuditor:
        name = "auditor"

        async def run(self, payload: AuditorInput, /) -> AgentResult[AuditReport]:
            captured.append(payload)
            return _ok_result(
                _audit_report(
                    hypothesis_run_id=payload.hypothesis.metadata.run_id,
                    backtest_run_id=payload.backtest.metadata.run_id,
                ),
                agent="auditor",
            )

    obs = _observer_artifact()
    hyp = _hypothesis_card()
    sk = _skeptic_report(
        verdict=SkepticVerdict.PROCEED,
        hypothesis_run_id=hyp.metadata.run_id,
    )
    rp = _research_plan(hypothesis_run_id=hyp.metadata.run_id)
    runner = CycleRunner(
        observer=_StubAgent(name="observer", result=_ok_result(obs, agent="observer")),
        hypothesis=_StubAgent(name="hypothesis", result=_ok_result(hyp, agent="hypothesis")),
        skeptic=_StubAgent(name="skeptic", result=_ok_result(sk, agent="skeptic")),
        research=_StubAgent(name="research", result=_ok_result(rp, agent="research")),
        auditor=_CapturingAuditor(),
        decider=_StubAgent(
            name="decider",
            result=_ok_result(
                _strategy_decision(backtest_run_id=_meta(agent="backtest").run_id),
                agent="decider",
            ),
        ),
        backtest_executor=lambda plan: _async_return(
            _backtest_report(plan_run_id=plan.metadata.run_id),
        ),
        event_log=EventLog(),
        budget=CycleBudget(
            cycle_id=new_cycle_id(),
            token_cap=10_000,
            wallclock_seconds_cap=60.0,
        ),
    )
    await runner.run(_observer_input())
    assert len(captured) == 1
    assert captured[0].hypothesis.metadata.run_id == hyp.metadata.run_id
    assert captured[0].backtest.plan_run_id == rp.metadata.run_id


# === Auto-persist via cycle_store ===


class _RecordingStore:
    """In-memory CycleStoreProtocol implementation for assertion."""

    def __init__(self) -> None:
        self.saves: list[tuple[CycleResult, tuple[object, ...]]] = []

    def save(self, *, result: CycleResult, events: object) -> object:
        self.saves.append((result, tuple(events)))  # type: ignore[arg-type]
        return None  # not a Path; runner does not consume return value


@pytest.mark.asyncio
async def test_auto_persist_saves_cycle_when_store_provided() -> None:
    store = _RecordingStore()
    log = EventLog()
    runner = _build_runner(event_log=log, cycle_store=store)
    result = await runner.run(_observer_input())

    assert len(store.saves) == 1
    saved_result, saved_events = store.saves[0]
    assert saved_result == result
    # All events for this cycle were forwarded:
    assert len(saved_events) == len(log.filter_by_cycle(result.cycle_id))


@pytest.mark.asyncio
async def test_auto_persist_only_includes_events_for_this_cycle() -> None:
    """A shared EventLog with prior unrelated events does not pollute the saved cycle."""
    log = EventLog()
    # Pre-populate with a different cycle's events
    other_cycle_id = new_cycle_id()
    log.append(cycle_started_event(cycle_id=other_cycle_id))

    store = _RecordingStore()
    runner = _build_runner(event_log=log, cycle_store=store)
    result = await runner.run(_observer_input())

    assert len(store.saves) == 1
    _, saved_events = store.saves[0]
    assert all(e.cycle_id == result.cycle_id for e in saved_events)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_auto_persist_records_aborted_cycle() -> None:
    store = _RecordingStore()
    log = EventLog()
    runner = _build_runner(event_log=log, cycle_store=store, observer_error="boom")
    result = await runner.run(_observer_input())

    assert result.aborted_reason is not None
    assert len(store.saves) == 1
    saved_result, _ = store.saves[0]
    assert saved_result.aborted_reason == result.aborted_reason


@pytest.mark.asyncio
async def test_no_persist_when_store_is_none() -> None:
    """Runner without a cycle_store still completes successfully."""
    runner = _build_runner()  # default cycle_store=None
    result = await runner.run(_observer_input())
    assert result.aborted_reason is None  # smoke test only


@pytest.mark.asyncio
async def test_real_cycle_store_round_trips_via_auto_persist(tmp_path: object) -> None:
    """End-to-end with the actual CycleStore implementation."""
    store = CycleStore(root=tmp_path)  # type: ignore[arg-type]
    log = EventLog()
    runner = _build_runner(event_log=log, cycle_store=store)
    result = await runner.run(_observer_input())

    loaded_result, loaded_events = store.load(result.cycle_id)
    assert loaded_result == result
    assert len(loaded_events) == len(log.filter_by_cycle(result.cycle_id))


# === Decider routing ===


@pytest.mark.asyncio
async def test_audit_fail_skips_decider() -> None:
    """A FAIL audit terminates at AUDITING without invoking the Decider."""
    hyp = _hypothesis_card()
    backtest_run_id = _meta(agent="backtest").run_id
    failing_audit = _audit_report(
        hypothesis_run_id=hyp.metadata.run_id,
        backtest_run_id=backtest_run_id,
        verdict=AuditVerdict.FAIL,
    )
    log = EventLog()
    runner = _build_runner(
        hypothesis_card=hyp,
        audit_report=failing_audit,
        event_log=log,
    )
    result = await runner.run(_observer_input())
    assert result.aborted_reason is None
    assert result.terminal_state is OrchestratorState.AUDITING
    assert result.artifacts.audit is not None
    assert result.artifacts.decision is None

    invoked_names = [
        e.payload["agent_name"] for e in log.filter_by_kind(CycleEventKind.AGENT_INVOKED)
    ]
    assert "decider" not in invoked_names


@pytest.mark.asyncio
async def test_decider_input_carries_audit_and_backtest() -> None:
    captured: list[DeciderInput] = []

    class _CapturingDecider:
        name = "decider"

        async def run(self, payload: DeciderInput, /) -> AgentResult[StrategyDecision]:
            captured.append(payload)
            return _ok_result(
                _strategy_decision(backtest_run_id=payload.backtest.metadata.run_id),
                agent="decider",
            )

    obs = _observer_artifact()
    hyp = _hypothesis_card()
    sk = _skeptic_report(
        verdict=SkepticVerdict.PROCEED,
        hypothesis_run_id=hyp.metadata.run_id,
    )
    rp = _research_plan(hypothesis_run_id=hyp.metadata.run_id)
    audit = _audit_report(
        hypothesis_run_id=hyp.metadata.run_id,
        backtest_run_id=_meta(agent="backtest").run_id,
    )
    runner = CycleRunner(
        observer=_StubAgent(name="observer", result=_ok_result(obs, agent="observer")),
        hypothesis=_StubAgent(name="hypothesis", result=_ok_result(hyp, agent="hypothesis")),
        skeptic=_StubAgent(name="skeptic", result=_ok_result(sk, agent="skeptic")),
        research=_StubAgent(name="research", result=_ok_result(rp, agent="research")),
        auditor=_StubAgent(name="auditor", result=_ok_result(audit, agent="auditor")),
        decider=_CapturingDecider(),
        backtest_executor=lambda plan: _async_return(
            _backtest_report(plan_run_id=plan.metadata.run_id),
        ),
        event_log=EventLog(),
        budget=CycleBudget(
            cycle_id=new_cycle_id(),
            token_cap=10_000,
            wallclock_seconds_cap=60.0,
        ),
    )
    await runner.run(_observer_input())
    assert len(captured) == 1
    assert captured[0].hypothesis.metadata.run_id == hyp.metadata.run_id
    assert captured[0].audit.metadata.run_id == audit.metadata.run_id


@pytest.mark.asyncio
async def test_decider_decision_action_can_be_reject() -> None:
    """A REJECT action still completes the cycle normally; decision artifact is recorded."""
    hyp = _hypothesis_card()
    backtest_run_id = _meta(agent="backtest").run_id
    rejected = _strategy_decision(
        backtest_run_id=backtest_run_id,
        action=DecisionAction.REJECT,
    )
    runner = _build_runner(hypothesis_card=hyp, decision=rejected)
    result = await runner.run(_observer_input())
    assert result.aborted_reason is None
    assert result.terminal_state is OrchestratorState.DECIDING
    assert result.artifacts.decision is not None
    assert result.artifacts.decision.action is DecisionAction.REJECT
