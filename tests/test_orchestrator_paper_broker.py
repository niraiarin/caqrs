"""P3.d-4 -- PaperBroker wired into CycleRunner after Policy Gateway."""

from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel

from caqrs.agents.protocol import AgentResult
from caqrs.execution.execution_report import ExecutionReport, ExecutionStatus
from caqrs.execution.live_broker_alpaca import LiveBrokerAlpaca
from caqrs.execution.paper_broker import PaperBroker
from caqrs.orchestrator import (
    CycleBudget,
    CycleEvent,
    CycleEventKind,
    CycleResult,
    CycleRunner,
    EventLog,
    OrchestratorState,
    new_cycle_id,
)
from caqrs.orchestrator.cycle_runner import CycleStoreProtocol, PriceProvider
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


def _skeptic_report(
    *,
    hypothesis_run_id: str,
    verdict: SkepticVerdict = SkepticVerdict.PROCEED,
) -> SkepticReport:
    if verdict is SkepticVerdict.KILL:
        return SkepticReport(
            metadata=_meta(agent="skeptic"),
            hypothesis_run_id=hypothesis_run_id,
            verdict=verdict,
            falsification_paths=(),
            concerns=("data leakage suspected",),
            summary="killed",
        )
    return SkepticReport(
        metadata=_meta(agent="skeptic"),
        hypothesis_run_id=hypothesis_run_id,
        verdict=verdict,
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


def _audit_report(
    *,
    hypothesis_run_id: str,
    backtest_run_id: str,
    verdict: AuditVerdict = AuditVerdict.PASS,
) -> AuditReport:
    passed = verdict is AuditVerdict.PASS
    return AuditReport(
        metadata=_meta(agent="auditor"),
        hypothesis_run_id=hypothesis_run_id,
        backtest_run_id=backtest_run_id,
        verdict=verdict,
        checks=(
            AcceptanceCheck(
                metric_path="aggregate.median_sharpe",
                op=">",
                threshold=Decimal("0.5"),
                actual=Decimal("1.2") if passed else Decimal("0.1"),
                passed=passed,
            ),
        ),
        rationale="all met" if passed else "below threshold",
    )


def _strategy_decision(
    *,
    backtest_run_id: str,
    action: DecisionAction = DecisionAction.ADOPT,
    targets: tuple[TargetPosition, ...] | None = None,
    notional_cap_usd: Decimal = Decimal("10000"),
) -> StrategyDecision:
    if targets is None:
        targets = (
            (
                TargetPosition(ticker="AAPL", side=Side.BUY, weight=Decimal("0.5")),
                TargetPosition(ticker="MSFT", side=Side.BUY, weight=Decimal("0.5")),
            )
            if action is DecisionAction.ADOPT
            else ()
        )
    return StrategyDecision(
        metadata=_meta(agent="decider"),
        backtest_run_id=backtest_run_id,
        action=action,
        targets=targets,
        rationale="ok",
        notional_cap_usd=notional_cap_usd,
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


class _SpyBroker:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(
        self,
        *,
        action: FeasibleAction,
        prices: dict[Ticker, Decimal],
    ) -> ExecutionReport:
        self.calls += 1
        return ExecutionReport(
            source_decision_run_id=action.source_decision_run_id,
            status=ExecutionStatus.SKIPPED,
            fills=(),
            reason="spy",
        )


class _SpyPriceProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, action: FeasibleAction) -> dict[Ticker, Decimal]:
        self.calls += 1
        return {target.ticker: Decimal("180") for target in action.targets}


class _RecordingStore:
    def __init__(self) -> None:
        self.saves: list[tuple[CycleResult, tuple[CycleEvent, ...]]] = []

    def save(self, *, result: CycleResult, events: Iterable[CycleEvent]) -> Path:
        self.saves.append((result, tuple(events)))
        return Path("/tmp/caqrs-recording-store")


async def _async_return[T](value: T) -> T:
    return value


async def _default_prices(action: FeasibleAction) -> dict[Ticker, Decimal]:
    return {target.ticker: Decimal("180") for target in action.targets}


def _make_runner(
    *,
    decision: StrategyDecision | None = None,
    policy_gateway_config: PolicyGatewayConfig | None = None,
    broker: PaperBroker | _SpyBroker | LiveBrokerAlpaca | None = None,
    price_provider: PriceProvider | None = None,
    event_log: EventLog | None = None,
    cycle_store: CycleStoreProtocol | None = None,
    skeptic_verdict: SkepticVerdict = SkepticVerdict.PROCEED,
    audit_verdict: AuditVerdict = AuditVerdict.PASS,
    decider_error: str | None = None,
) -> CycleRunner:
    obs = _observer_artifact()
    hyp = _hypothesis_card()
    sk = _skeptic_report(hypothesis_run_id=hyp.metadata.run_id, verdict=skeptic_verdict)
    rp = _research_plan(hypothesis_run_id=hyp.metadata.run_id)
    backtest_run_id = _meta(agent="backtest").run_id
    audit = _audit_report(
        hypothesis_run_id=hyp.metadata.run_id,
        backtest_run_id=backtest_run_id,
        verdict=audit_verdict,
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
        cycle_store=cycle_store,
        policy_gateway_config=policy_gateway_config,
        broker=broker,
        price_provider=price_provider,
    )


def _broker_events(log: EventLog) -> tuple[CycleEvent, ...]:
    return log.filter_by_kind(CycleEventKind.BROKER_EXECUTED)


@pytest.mark.asyncio
async def test_default_no_broker_preserves_no_execution_report() -> None:
    log = EventLog()
    runner = _make_runner(policy_gateway_config=PolicyGatewayConfig(), event_log=log)
    result = await runner.run(_observer_input())

    assert result.artifacts.execution_report is None
    assert _broker_events(log) == ()


@pytest.mark.asyncio
async def test_adopt_fills_and_emits_filled_event() -> None:
    log = EventLog()

    async def prices(action: FeasibleAction) -> dict[Ticker, Decimal]:
        return {"AAPL": Decimal("180"), "MSFT": Decimal("400")}

    runner = _make_runner(
        policy_gateway_config=PolicyGatewayConfig(),
        broker=PaperBroker(initial_capital_usd=Decimal("10000")),
        price_provider=prices,
        event_log=log,
    )
    result = await runner.run(_observer_input())

    report = result.artifacts.execution_report
    assert report is not None
    assert report.status is ExecutionStatus.FILLED
    assert len(report.fills) > 0
    broker_event = _broker_events(log)[0]
    assert broker_event.payload["status"] == "filled"
    assert broker_event.payload["fill_count"] == len(report.fills)


@pytest.mark.asyncio
async def test_defer_from_gateway_is_skipped_by_broker() -> None:
    log = EventLog()
    decision = _strategy_decision(
        backtest_run_id=_meta(agent="backtest").run_id,
        notional_cap_usd=Decimal("2000000"),
    )
    runner = _make_runner(
        decision=decision,
        policy_gateway_config=PolicyGatewayConfig(account_notional_cap_usd=Decimal("500000")),
        broker=PaperBroker(initial_capital_usd=Decimal("10000")),
        price_provider=_default_prices,
        event_log=log,
    )
    result = await runner.run(_observer_input())

    report = result.artifacts.execution_report
    assert report is not None
    assert report.status is ExecutionStatus.SKIPPED
    assert _broker_events(log)[0].payload["status"] == "skipped"


@pytest.mark.asyncio
async def test_reject_decision_is_skipped_by_broker() -> None:
    log = EventLog()
    decision = _strategy_decision(
        backtest_run_id=_meta(agent="backtest").run_id,
        action=DecisionAction.REJECT,
        targets=(),
    )
    runner = _make_runner(
        decision=decision,
        policy_gateway_config=PolicyGatewayConfig(),
        broker=PaperBroker(initial_capital_usd=Decimal("10000")),
        price_provider=_default_prices,
        event_log=log,
    )
    result = await runner.run(_observer_input())

    report = result.artifacts.execution_report
    assert report is not None
    assert report.status is ExecutionStatus.SKIPPED
    assert report.reason == "action=reject; no broker work"
    assert _broker_events(log)[0].payload["status"] == "skipped"


@pytest.mark.asyncio
async def test_adopt_with_missing_price_is_rejected_by_broker() -> None:
    log = EventLog()

    async def missing_aapl(action: FeasibleAction) -> dict[Ticker, Decimal]:
        return {"MSFT": Decimal("400")}

    targets = (TargetPosition(ticker="AAPL", side=Side.BUY, weight=Decimal("0.5")),)
    decision = _strategy_decision(
        backtest_run_id=_meta(agent="backtest").run_id,
        targets=targets,
    )
    runner = _make_runner(
        decision=decision,
        policy_gateway_config=PolicyGatewayConfig(),
        broker=PaperBroker(initial_capital_usd=Decimal("10000")),
        price_provider=missing_aapl,
        event_log=log,
    )
    result = await runner.run(_observer_input())

    report = result.artifacts.execution_report
    assert report is not None
    assert report.status is ExecutionStatus.REJECTED
    assert _broker_events(log)[0].payload["status"] == "rejected"


@pytest.mark.asyncio
async def test_broker_without_gateway_is_silently_skipped() -> None:
    log = EventLog()
    broker = _SpyBroker()
    runner = _make_runner(
        policy_gateway_config=None,
        broker=broker,
        price_provider=_default_prices,
        event_log=log,
    )
    result = await runner.run(_observer_input())

    assert broker.calls == 0
    assert result.artifacts.execution_report is None
    assert _broker_events(log) == ()


@pytest.mark.asyncio
async def test_broker_without_price_provider_is_silently_skipped() -> None:
    log = EventLog()
    broker = _SpyBroker()
    runner = _make_runner(
        policy_gateway_config=PolicyGatewayConfig(),
        broker=broker,
        price_provider=None,
        event_log=log,
    )
    result = await runner.run(_observer_input())

    assert broker.calls == 0
    assert result.artifacts.feasible_action is not None
    assert result.artifacts.execution_report is None
    assert _broker_events(log) == ()


@pytest.mark.asyncio
async def test_price_provider_without_broker_is_silently_skipped() -> None:
    log = EventLog()
    price_provider = _SpyPriceProvider()
    runner = _make_runner(
        policy_gateway_config=PolicyGatewayConfig(),
        broker=None,
        price_provider=price_provider,
        event_log=log,
    )
    result = await runner.run(_observer_input())

    assert price_provider.calls == 0
    assert result.artifacts.feasible_action is not None
    assert result.artifacts.execution_report is None
    assert _broker_events(log) == ()


@pytest.mark.asyncio
async def test_audit_fail_skips_broker() -> None:
    log = EventLog()
    broker = _SpyBroker()
    runner = _make_runner(
        policy_gateway_config=PolicyGatewayConfig(),
        broker=broker,
        price_provider=_default_prices,
        event_log=log,
        audit_verdict=AuditVerdict.FAIL,
    )
    result = await runner.run(_observer_input())

    assert broker.calls == 0
    assert result.artifacts.decision is None
    assert result.artifacts.execution_report is None
    assert _broker_events(log) == ()


@pytest.mark.asyncio
async def test_skeptic_non_proceed_skips_broker() -> None:
    log = EventLog()
    broker = _SpyBroker()
    runner = _make_runner(
        policy_gateway_config=PolicyGatewayConfig(),
        broker=broker,
        price_provider=_default_prices,
        event_log=log,
        skeptic_verdict=SkepticVerdict.KILL,
    )
    result = await runner.run(_observer_input())

    assert broker.calls == 0
    assert result.terminal_state is OrchestratorState.SCRUTINIZING
    assert result.artifacts.execution_report is None
    assert _broker_events(log) == ()


@pytest.mark.asyncio
async def test_decider_failure_skips_broker() -> None:
    log = EventLog()
    broker = _SpyBroker()
    runner = _make_runner(
        policy_gateway_config=PolicyGatewayConfig(),
        broker=broker,
        price_provider=_default_prices,
        event_log=log,
        decider_error="decider failed",
    )
    result = await runner.run(_observer_input())

    assert broker.calls == 0
    assert result.terminal_state is OrchestratorState.ERROR
    assert result.artifacts.execution_report is None
    assert _broker_events(log) == ()


@pytest.mark.asyncio
async def test_broker_event_is_ordered_between_gateway_and_completion() -> None:
    log = EventLog()
    runner = _make_runner(
        policy_gateway_config=PolicyGatewayConfig(),
        broker=PaperBroker(initial_capital_usd=Decimal("10000")),
        price_provider=_default_prices,
        event_log=log,
    )
    await runner.run(_observer_input())

    kinds = [event.kind for event in log.events]
    gateway_idx = kinds.index(CycleEventKind.POLICY_GATEWAY_APPLIED)
    broker_idx = kinds.index(CycleEventKind.BROKER_EXECUTED)
    completed_idx = kinds.index(CycleEventKind.CYCLE_COMPLETED)
    assert gateway_idx < broker_idx < completed_idx


@pytest.mark.asyncio
async def test_price_provider_error_aborts_without_broker_event() -> None:
    log = EventLog()

    async def raises(action: FeasibleAction) -> dict[Ticker, Decimal]:
        raise RuntimeError("boom")

    runner = _make_runner(
        policy_gateway_config=PolicyGatewayConfig(),
        broker=PaperBroker(initial_capital_usd=Decimal("10000")),
        price_provider=raises,
        event_log=log,
    )
    result = await runner.run(_observer_input())

    assert result.terminal_state is OrchestratorState.ERROR
    assert result.aborted_reason is not None
    assert "price_provider" in result.aborted_reason
    assert "boom" in result.aborted_reason
    assert log.filter_by_kind(CycleEventKind.CYCLE_ABORTED) != ()
    assert _broker_events(log) == ()


@pytest.mark.asyncio
async def test_execution_report_preserves_source_decision_run_id() -> None:
    runner = _make_runner(
        policy_gateway_config=PolicyGatewayConfig(),
        broker=PaperBroker(initial_capital_usd=Decimal("10000")),
        price_provider=_default_prices,
    )
    result = await runner.run(_observer_input())

    report = result.artifacts.execution_report
    decision = result.artifacts.decision
    assert report is not None
    assert decision is not None
    assert report.source_decision_run_id == decision.metadata.run_id


@pytest.mark.asyncio
async def test_cycle_store_receives_broker_event() -> None:
    log = EventLog()
    store = _RecordingStore()
    runner = _make_runner(
        policy_gateway_config=PolicyGatewayConfig(),
        broker=PaperBroker(initial_capital_usd=Decimal("10000")),
        price_provider=_default_prices,
        event_log=log,
        cycle_store=store,
    )
    result = await runner.run(_observer_input())

    assert len(store.saves) == 1
    saved_result, saved_events = store.saves[0]
    assert saved_result == result
    kinds = [event.kind for event in saved_events]
    assert CycleEventKind.BROKER_EXECUTED in kinds


# === LiveBrokerAlpaca wiring (NFR-LIVE-BROKER-7) =============================


@pytest.mark.asyncio
async def test_live_broker_emits_broker_live_rejected_with_runner_cycle_id() -> None:
    """NFR-LIVE-BROKER-7 + runner integration: when CycleRunner reaches
    the broker step with a LiveBrokerAlpaca, the broker MUST emit a
    BROKER_LIVE_REJECTED event (default-off short-circuit) into the
    same EventLog the runner uses, AND the events MUST carry the
    runners current cycle_id (not the cycle_id passed to the broker
    constructor). The runner MUST NOT emit BROKER_EXECUTED for live
    brokers — the BROKER_LIVE_* taxonomy is authoritative."""
    log = EventLog()
    paper = PaperBroker(initial_capital_usd=Decimal("10000"))
    # No cycle_id / event_log at construction — the runner injects them
    # via attach_cycle_context per-cycle (Codex audit refactor).
    live = LiveBrokerAlpaca(
        paper_broker=paper,
        live_broker_daily_loss_cap_usd=Decimal("1000"),
    )
    runner = _make_runner(
        policy_gateway_config=PolicyGatewayConfig(),
        broker=live,
        price_provider=_default_prices,
        event_log=log,
    )
    runner_cycle_id = runner._budget.cycle_id

    await runner.run(_observer_input())

    rejected = log.filter_by_kind(CycleEventKind.BROKER_LIVE_REJECTED)
    assert len(rejected) == 1
    assert rejected[0].cycle_id == runner_cycle_id
    assert rejected[0].payload["reason"] == "live orders disabled"
    # Runner MUST NOT have emitted BROKER_EXECUTED for the live broker.
    assert log.filter_by_kind(CycleEventKind.BROKER_EXECUTED) == ()


@pytest.mark.asyncio
async def test_live_broker_kill_switch_engaged_emits_rejected_not_executed() -> None:
    """Kill switch engaged BEFORE the runner reaches the broker step:
    LiveBroker.execute() short-circuits with reason 'kill switch engaged'
    and emits BROKER_LIVE_REJECTED into the runner's log. The runner
    MUST NOT emit BROKER_EXECUTED for the live broker."""
    log = EventLog()
    paper = PaperBroker(initial_capital_usd=Decimal("10000"))
    live = LiveBrokerAlpaca(
        paper_broker=paper,
        live_broker_daily_loss_cap_usd=Decimal("1000"),
    )
    live.kill_switch()  # engage outside any cycle context
    runner = _make_runner(
        policy_gateway_config=PolicyGatewayConfig(),
        broker=live,
        price_provider=_default_prices,
        event_log=log,
    )
    await runner.run(_observer_input())
    rejected = log.filter_by_kind(CycleEventKind.BROKER_LIVE_REJECTED)
    assert len(rejected) == 1
    assert rejected[0].payload["reason"] == "kill switch engaged"
    assert log.filter_by_kind(CycleEventKind.BROKER_EXECUTED) == ()


@pytest.mark.asyncio
async def test_live_broker_attach_detach_pairs_per_execute() -> None:
    """attach_cycle_context is non-reentrant: a second attach without a
    detach in between MUST raise RuntimeError. This regression-guards
    the cycle attribution invariant Codex audit 2026-05-09 finding 2
    flagged."""
    paper = PaperBroker(initial_capital_usd=Decimal("10000"))
    live = LiveBrokerAlpaca(
        paper_broker=paper,
        live_broker_daily_loss_cap_usd=Decimal("1000"),
    )
    log = EventLog()
    live.attach_cycle_context(cycle_id="cycle1", event_log=log)
    with pytest.raises(RuntimeError, match="non-reentrant"):
        live.attach_cycle_context(cycle_id="cycle2", event_log=log)
    live.detach_cycle_context()
    # After detach, another attach succeeds — not stuck.
    live.attach_cycle_context(cycle_id="cycle3", event_log=log)
