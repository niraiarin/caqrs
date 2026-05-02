"""Correlation-ID propagation tests for the cycle event log (NFR-OBS-3).

Locks down the structured-log schema documented in
``docs/observability-spec.md``:

* every event in a cycle's event log carries a stable ``cycle_id``
  (Pydantic ``min_length=1``) and a unique ``event_id``;
* every ``AGENT_INVOKED`` / ``AGENT_SUCCEEDED`` / ``AGENT_FAILED`` event
  carries an ``agent_name`` and ``run_id``, and the ``run_id`` is paired
  across the invoke/succeed (or invoke/fail) edge for the same invocation;
* ``POLICY_GATEWAY_APPLIED.decision_run_id`` ==
  ``BROKER_EXECUTED.decision_run_id`` ==
  ``ExecutionReport.source_decision_run_id`` ==
  ``StrategyDecision.metadata.run_id`` — the "decision-trace" chain that
  an auditor follows end-to-end.

Note on the *invocation* ``run_id`` vs the *artifact* ``run_id``: the
runner allocates a fresh ``run_id`` for every ``AGENT_INVOKED`` event so
that an invocation is identifiable even before the agent returns. The
agent independently allocates its own ``run_id`` inside the artifact's
``RunMetadata``. The decision-trace chain uses the artifact's
``metadata.run_id`` (the one that survives in storage), not the
invocation's transient id. Both ids are non-empty strings of equal
shape and live side by side in the log; downstream consumers correlate
on the artifact id.

The tests reuse the stub-agent + factory pattern from
``tests/test_orchestrator_paper_broker.py`` so the cycle is fully
deterministic with no LLM calls.
"""

from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import BaseModel

from caqrs.agents.protocol import AgentResult
from caqrs.execution.paper_broker import PaperBroker
from caqrs.orchestrator import (
    CycleBudget,
    CycleEvent,
    CycleEventKind,
    CycleRunner,
    EventLog,
    OrchestratorState,
    new_cycle_id,
)
from caqrs.policy.gateway import FeasibleAction, PolicyGatewayConfig
from caqrs.schemas.audit import AcceptanceCheck, AuditReport, AuditVerdict
from caqrs.schemas.backtest_report import (
    AggregateMetrics,
    BacktestReport,
    FoldMetrics,
)
from caqrs.schemas.common import RunMetadata, Ticker, new_run_id
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

# ---------------------------------------------------------------------------
# Artifact factories (mirror tests/test_orchestrator_paper_broker.py)
# ---------------------------------------------------------------------------


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
        universe=("AAPL", "MSFT"),
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        horizon_days=30,
        dimensions=(DataDimension.PRICES,),
    )


def _observer_artifact() -> ObserverArtifact:
    return ObserverArtifact(
        metadata=_meta(agent="observer"),
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


def _hypothesis_card() -> HypothesisCard:
    return HypothesisCard(
        metadata=_meta(agent="hypothesis"),
        status=HypothesisStatus.DRAFT,
        claim="AAPL outperforms MSFT over the next 30 days.",
        rationale="trend persistence",
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


def _skeptic_report(*, hypothesis_run_id: str) -> SkepticReport:
    return SkepticReport(
        metadata=_meta(agent="skeptic"),
        hypothesis_run_id=hypothesis_run_id,
        verdict=SkepticVerdict.PROCEED,
        falsification_paths=(),
        concerns=(),
        summary="no major red flags",
    )


def _research_plan(*, hypothesis_run_id: str) -> ResearchPlan:
    return ResearchPlan(
        metadata=_meta(agent="research"),
        hypothesis_run_id=hypothesis_run_id,
        universe=("AAPL", "MSFT"),
        frequency=DataFrequency.DAILY,
        walk_forward=(
            WalkForwardWindow(
                train_start=datetime(2025, 1, 1, tzinfo=UTC),
                train_end=datetime(2025, 5, 31, tzinfo=UTC),
                test_start=datetime(2025, 6, 1, tzinfo=UTC),
                test_end=datetime(2025, 6, 30, tzinfo=UTC),
            ),
        ),
        cost_model_bps=Decimal(0),
        slippage_bps=Decimal(0),
        seed=1,
    )


def _backtest_report(*, plan_run_id: str) -> BacktestReport:
    return BacktestReport(
        metadata=_meta(agent="backtest"),
        plan_run_id=plan_run_id,
        folds=(
            FoldMetrics(
                fold_index=0,
                test_start=datetime(2025, 6, 1, tzinfo=UTC),
                test_end=datetime(2025, 6, 30, tzinfo=UTC),
                sharpe=Decimal("1.2"),
                pnl_usd=Decimal("100"),
                max_drawdown_pct=Decimal("0.5"),
                turnover=Decimal("0.1"),
                n_trades=2,
            ),
        ),
        aggregate=AggregateMetrics(
            median_sharpe=Decimal("1.2"),
            mean_sharpe=Decimal("1.2"),
            worst_fold_sharpe=Decimal("1.2"),
            median_max_drawdown_pct=Decimal("0.5"),
            total_pnl_usd=Decimal("100"),
            total_trades=2,
        ),
    )


def _audit_report(*, hypothesis_run_id: str, backtest_run_id: str) -> AuditReport:
    return AuditReport(
        metadata=_meta(agent="auditor"),
        hypothesis_run_id=hypothesis_run_id,
        backtest_run_id=backtest_run_id,
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
        rationale="all met",
    )


def _strategy_decision(*, backtest_run_id: str) -> StrategyDecision:
    return StrategyDecision(
        metadata=_meta(agent="decider"),
        backtest_run_id=backtest_run_id,
        action=DecisionAction.ADOPT,
        targets=(
            TargetPosition(ticker="AAPL", side=Side.BUY, weight=Decimal("0.5")),
            TargetPosition(ticker="MSFT", side=Side.BUY, weight=Decimal("0.5")),
        ),
        rationale="ok",
        notional_cap_usd=Decimal("10000"),
        max_position_weight=Decimal("0.7"),
        daily_loss_limit_usd=Decimal("500"),
    )


class _StubAgent[I: BaseModel, O: BaseModel]:
    def __init__(self, *, name: str, result: AgentResult[O]) -> None:
        self.name = name
        self._result = result

    async def run(self, payload: I, /) -> AgentResult[O]:
        return self._result


def _ok_result[O: BaseModel](output: O, *, agent: str) -> AgentResult[O]:
    return AgentResult[O](
        output=output,
        error=None,
        metadata=_meta(agent=agent),
    )


def _fail_result[O: BaseModel](*, agent: str, error: str) -> AgentResult[O]:
    return AgentResult[O](
        output=None,
        error=error,
        metadata=_meta(agent=agent),
    )


async def _default_prices(action: FeasibleAction) -> dict[Ticker, Decimal]:
    return {target.ticker: Decimal("180") for target in action.targets}


async def _async_return[T](value: T) -> T:
    return value


def _make_runner(
    *,
    decision: StrategyDecision | None = None,
    event_log: EventLog | None = None,
    decider_error: str | None = None,
) -> CycleRunner:
    """Build a fully-stubbed CycleRunner.

    Defaults to a happy-path adopt cycle wired through the policy
    gateway and a real PaperBroker so every event-emitting branch is
    exercised. ``decider_error`` simulates an agent failure to test
    the abort path.
    """
    obs = _observer_artifact()
    hyp = _hypothesis_card()
    sk = _skeptic_report(hypothesis_run_id=hyp.metadata.run_id)
    rp = _research_plan(hypothesis_run_id=hyp.metadata.run_id)
    backtest_run_id = _meta(agent="backtest").run_id
    audit = _audit_report(
        hypothesis_run_id=hyp.metadata.run_id,
        backtest_run_id=backtest_run_id,
    )
    decision_result: AgentResult[StrategyDecision] = (
        _fail_result(agent="decider", error=decider_error)
        if decider_error is not None
        else _ok_result(
            decision
            if decision is not None
            else _strategy_decision(backtest_run_id=backtest_run_id),
            agent="decider",
        )
    )

    async def bt_executor(plan: ResearchPlan) -> BacktestReport:
        return await _async_return(_backtest_report(plan_run_id=plan.metadata.run_id))

    log = event_log if event_log is not None else EventLog()
    return CycleRunner(
        observer=_StubAgent(name="observer", result=_ok_result(obs, agent="observer")),
        hypothesis=_StubAgent(name="hypothesis", result=_ok_result(hyp, agent="hypothesis")),
        skeptic=_StubAgent(name="skeptic", result=_ok_result(sk, agent="skeptic")),
        research=_StubAgent(name="research", result=_ok_result(rp, agent="research")),
        auditor=_StubAgent(name="auditor", result=_ok_result(audit, agent="auditor")),
        decider=_StubAgent(name="decider", result=decision_result),
        backtest_executor=bt_executor,
        event_log=log,
        budget=CycleBudget(
            cycle_id=new_cycle_id(),
            token_cap=10_000,
            wallclock_seconds_cap=60.0,
        ),
        policy_gateway_config=PolicyGatewayConfig(),
        broker=PaperBroker(initial_capital_usd=Decimal("10000")),
        price_provider=_default_prices,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Per-event-kind required payload keys. The first three universal fields
# (event_id, cycle_id, timestamp) are checked separately because they
# live on the CycleEvent envelope, not in the payload dict.
_REQUIRED_PAYLOAD_KEYS: dict[CycleEventKind, frozenset[str]] = {
    CycleEventKind.AGENT_INVOKED: frozenset({"agent_name", "run_id"}),
    CycleEventKind.AGENT_SUCCEEDED: frozenset({"agent_name", "run_id"}),
    CycleEventKind.AGENT_FAILED: frozenset({"agent_name", "run_id"}),
    CycleEventKind.POLICY_GATEWAY_APPLIED: frozenset({"decision_run_id"}),
    CycleEventKind.BROKER_EXECUTED: frozenset({"decision_run_id"}),
}


def _only(events: Iterable[CycleEvent], kind: CycleEventKind) -> CycleEvent:
    matches = [e for e in events if e.kind is kind]
    assert len(matches) == 1, f"expected exactly one {kind} event, got {len(matches)}"
    return matches[0]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.traces("NFR-OBS-3")
async def test_all_events_share_cycle_id() -> None:
    """Every event in a happy-path cycle log carries the same cycle_id."""
    log = EventLog()
    runner = _make_runner(event_log=log)
    result = await runner.run(_observer_input())

    assert result.aborted_reason is None
    assert len(log.events) > 0
    for event in log.events:
        assert event.cycle_id == result.cycle_id
        # Pydantic Field(min_length=1) already rejects empty strings,
        # but the schema invariant is asserted here for the spec.
        assert event.cycle_id != ""


@pytest.mark.asyncio
@pytest.mark.traces("NFR-OBS-3")
async def test_all_event_ids_are_unique() -> None:
    """Every event in a cycle log gets a fresh event_id."""
    log = EventLog()
    runner = _make_runner(event_log=log)
    await runner.run(_observer_input())

    ids = [e.event_id for e in log.events]
    assert len(ids) == len(set(ids)), f"duplicate event_id detected in {ids}"
    for event in log.events:
        assert event.event_id != ""


@pytest.mark.asyncio
@pytest.mark.traces("NFR-OBS-3")
async def test_all_event_timestamps_are_tz_aware_utc() -> None:
    """Every event timestamp is tz-aware and pinned to UTC."""
    log = EventLog()
    runner = _make_runner(event_log=log)
    await runner.run(_observer_input())

    for event in log.events:
        assert event.timestamp.tzinfo is not None
        # Compare offsets, not identity: any tz with offset 0 is acceptable
        # but the constructors in events.py use UTC explicitly.
        assert event.timestamp.utcoffset() == datetime.now(UTC).utcoffset()


@pytest.mark.asyncio
@pytest.mark.traces("NFR-OBS-3")
async def test_per_event_kind_required_payload_fields_present() -> None:
    """Every event of a tracked kind carries its required payload fields.

    Property-style: walks the entire event log and asserts no event of
    a tracked kind is missing any of its required correlation fields.
    """
    log = EventLog()
    runner = _make_runner(event_log=log)
    await runner.run(_observer_input())

    for event in log.events:
        required = _REQUIRED_PAYLOAD_KEYS.get(event.kind)
        if required is None:
            continue
        missing = required - event.payload.keys()
        assert not missing, f"{event.kind} event missing payload keys: {missing}"
        for key in required:
            value = event.payload[key]
            assert isinstance(value, str), (
                f"{event.kind}.payload[{key!r}] must be a string, got {type(value).__name__}"
            )
            assert value, f"{event.kind}.payload[{key!r}] must be non-empty"


@pytest.mark.asyncio
@pytest.mark.traces("NFR-OBS-3")
async def test_agent_invoked_succeeded_run_ids_pair_per_invocation() -> None:
    """For each agent, AGENT_INVOKED.run_id == AGENT_SUCCEEDED.run_id.

    The runner-allocated invocation ``run_id`` is what binds the
    invoke/succeed edge of a single agent call together. This is the
    "invocation id" axis of the correlation schema; the artifact
    ``metadata.run_id`` is a separate axis (see the module docstring).
    """
    log = EventLog()
    runner = _make_runner(event_log=log)
    await runner.run(_observer_input())

    invoked = log.filter_by_kind(CycleEventKind.AGENT_INVOKED)
    succeeded = log.filter_by_kind(CycleEventKind.AGENT_SUCCEEDED)
    # Happy path: every invoke that was followed by a success has its
    # run_id mirrored on the succeeded event for the same agent_name.
    by_agent_invoked = {e.payload["agent_name"]: e.payload["run_id"] for e in invoked}
    by_agent_succeeded = {e.payload["agent_name"]: e.payload["run_id"] for e in succeeded}
    assert by_agent_invoked.keys() == by_agent_succeeded.keys()
    for agent_name, invoked_run_id in by_agent_invoked.items():
        assert by_agent_succeeded[agent_name] == invoked_run_id, (
            f"{agent_name}: AGENT_INVOKED.run_id={invoked_run_id} but "
            f"AGENT_SUCCEEDED.run_id={by_agent_succeeded[agent_name]}"
        )


@pytest.mark.asyncio
@pytest.mark.traces("NFR-OBS-3")
async def test_gateway_decision_run_id_propagates_to_broker_event() -> None:
    """POLICY_GATEWAY_APPLIED.decision_run_id == BROKER_EXECUTED.decision_run_id."""
    log = EventLog()
    runner = _make_runner(event_log=log)
    await runner.run(_observer_input())

    gateway_event = _only(log.events, CycleEventKind.POLICY_GATEWAY_APPLIED)
    broker_event = _only(log.events, CycleEventKind.BROKER_EXECUTED)

    assert gateway_event.payload["decision_run_id"] == broker_event.payload["decision_run_id"]


@pytest.mark.asyncio
@pytest.mark.traces("NFR-OBS-3")
async def test_execution_report_source_decision_run_id_matches_decision() -> None:
    """ExecutionReport.source_decision_run_id == StrategyDecision.metadata.run_id.

    Closes the decision-trace chain on the artifact-id axis:
    POLICY_GATEWAY_APPLIED.decision_run_id ==
    BROKER_EXECUTED.decision_run_id ==
    ExecutionReport.source_decision_run_id ==
    StrategyDecision.metadata.run_id (all the same id).
    """
    log = EventLog()
    runner = _make_runner(event_log=log)
    result = await runner.run(_observer_input())

    decision = result.artifacts.decision
    report = result.artifacts.execution_report
    assert decision is not None
    assert report is not None
    assert report.source_decision_run_id == decision.metadata.run_id

    # Cross-check against the events for the full chain.
    gateway_event = _only(log.events, CycleEventKind.POLICY_GATEWAY_APPLIED)
    broker_event = _only(log.events, CycleEventKind.BROKER_EXECUTED)
    assert gateway_event.payload["decision_run_id"] == decision.metadata.run_id
    assert broker_event.payload["decision_run_id"] == decision.metadata.run_id


@pytest.mark.asyncio
@pytest.mark.traces("NFR-OBS-3")
async def test_aborted_cycle_still_carries_cycle_id_on_every_emitted_event() -> None:
    """Abort path preserves the cycle_id invariant on every emitted event.

    A decider failure aborts the cycle before the policy gateway and
    broker run, so the trace is shorter — but every event that *was*
    emitted must still carry the correct cycle_id, and AGENT_FAILED
    must carry agent_name + run_id.
    """
    log = EventLog()
    runner = _make_runner(event_log=log, decider_error="decider blew up")
    result = await runner.run(_observer_input())

    assert result.terminal_state is OrchestratorState.ERROR
    assert result.aborted_reason is not None
    assert len(log.events) > 0

    for event in log.events:
        assert event.cycle_id == result.cycle_id

    # The cycle aborted at the decider, so no gateway / broker events
    # should have been emitted.
    assert log.filter_by_kind(CycleEventKind.POLICY_GATEWAY_APPLIED) == ()
    assert log.filter_by_kind(CycleEventKind.BROKER_EXECUTED) == ()

    # AGENT_FAILED for the decider must still carry its correlation
    # fields so the abort itself is auditable.
    failed = _only(log.events, CycleEventKind.AGENT_FAILED)
    assert failed.payload["agent_name"] == "decider"
    assert failed.payload["run_id"]
