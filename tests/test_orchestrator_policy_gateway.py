"""P3.c — Policy Gateway wired into CycleRunner.

When ``policy_gateway_config`` is supplied to the runner, an ADOPT
decision passes through :func:`apply_policy_gateway` before
``CYCLE_COMPLETED``. The projected :class:`FeasibleAction` is recorded
on :class:`CycleArtifacts.feasible_action`; a
``POLICY_GATEWAY_APPLIED`` event is emitted with the violation count
and final action.

Default behaviour (``policy_gateway_config=None``) is unchanged: no
gateway invocation, no event, ``feasible_action`` left ``None``. This
keeps the wiring a conservative extension over P1.4 / P2 cycle tests.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import BaseModel

from caqrs.agents.auditor import AuditorInput  # noqa: F401
from caqrs.agents.decider import DeciderInput  # noqa: F401
from caqrs.agents.protocol import AgentResult
from caqrs.agents.research import ResearchInput  # noqa: F401
from caqrs.orchestrator import (
    CycleBudget,
    CycleEventKind,
    CycleRunner,
    EventLog,
    new_cycle_id,
)
from caqrs.policy.gateway import (
    FeasibleAction,
    PolicyGatewayConfig,
    PolicyViolationKind,
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

# === Minimal artifact factories (kept local to avoid coupling to the main
# runner test module). Identical shape, abridged. ===


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


def _strategy_decision(
    *,
    backtest_run_id: str,
    action: DecisionAction = DecisionAction.ADOPT,
    targets: tuple[TargetPosition, ...] | None = None,
    notional_cap_usd: Decimal = Decimal("10000"),
) -> StrategyDecision:
    if targets is None:
        targets = (
            (TargetPosition(ticker="AAPL", side=Side.BUY, weight=Decimal("0.5")),)
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


# === Stub agents ===


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


async def _async_return[T](value: T) -> T:
    return value


def _make_runner(
    *,
    decision: StrategyDecision,
    policy_gateway_config: PolicyGatewayConfig | None,
    event_log: EventLog | None = None,
) -> CycleRunner:
    obs = _observer_artifact()
    hyp = _hypothesis_card()
    sk = _skeptic_report(hypothesis_run_id=hyp.metadata.run_id)
    rp = _research_plan(hypothesis_run_id=hyp.metadata.run_id)
    backtest_run_id = _meta(agent="backtest").run_id
    audit = _audit_report(
        hypothesis_run_id=hyp.metadata.run_id,
        backtest_run_id=backtest_run_id,
    )

    log = event_log if event_log is not None else EventLog()
    bg = CycleBudget(
        cycle_id=new_cycle_id(),
        token_cap=10_000,
        wallclock_seconds_cap=60.0,
    )

    async def bt_executor(plan: ResearchPlan) -> BacktestReport:
        return _backtest_report(plan_run_id=plan.metadata.run_id)

    return CycleRunner(
        observer=_StubAgent(name="observer", result=_ok_result(obs, agent="observer")),
        hypothesis=_StubAgent(name="hypothesis", result=_ok_result(hyp, agent="hypothesis")),
        skeptic=_StubAgent(name="skeptic", result=_ok_result(sk, agent="skeptic")),
        research=_StubAgent(name="research", result=_ok_result(rp, agent="research")),
        auditor=_StubAgent(name="auditor", result=_ok_result(audit, agent="auditor")),
        decider=_StubAgent(name="decider", result=_ok_result(decision, agent="decider")),
        backtest_executor=bt_executor,
        event_log=log,
        budget=bg,
        policy_gateway_config=policy_gateway_config,
    )


# === Tests ===


@pytest.mark.asyncio
async def test_no_config_means_no_gateway_invocation() -> None:
    """Default behaviour preserved: no config → no FeasibleAction, no event."""
    log = EventLog()
    decision = _strategy_decision(backtest_run_id=_meta(agent="backtest").run_id)
    runner = _make_runner(decision=decision, policy_gateway_config=None, event_log=log)
    result = await runner.run(_observer_input())

    assert result.artifacts.feasible_action is None
    applied = log.filter_by_kind(CycleEventKind.POLICY_GATEWAY_APPLIED)
    assert applied == ()


@pytest.mark.asyncio
async def test_empty_config_passes_adopt_through_unchanged() -> None:
    log = EventLog()
    decision = _strategy_decision(backtest_run_id=_meta(agent="backtest").run_id)
    cfg = PolicyGatewayConfig()
    runner = _make_runner(decision=decision, policy_gateway_config=cfg, event_log=log)
    result = await runner.run(_observer_input())

    fa = result.artifacts.feasible_action
    assert isinstance(fa, FeasibleAction)
    assert fa.action is DecisionAction.ADOPT
    assert fa.targets == decision.targets
    assert fa.violations == ()


@pytest.mark.asyncio
async def test_violating_decision_demotes_to_defer_and_emits_event() -> None:
    log = EventLog()
    decision = _strategy_decision(
        backtest_run_id=_meta(agent="backtest").run_id,
        notional_cap_usd=Decimal("2000000"),
    )
    cfg = PolicyGatewayConfig(account_notional_cap_usd=Decimal("500000"))
    runner = _make_runner(decision=decision, policy_gateway_config=cfg, event_log=log)
    result = await runner.run(_observer_input())

    fa = result.artifacts.feasible_action
    assert isinstance(fa, FeasibleAction)
    assert fa.action is DecisionAction.DEFER
    assert fa.targets == ()
    kinds = {v.kind for v in fa.violations}
    assert PolicyViolationKind.NOTIONAL_CAP_EXCEEDED in kinds

    # Event captures the projection outcome.
    applied = log.filter_by_kind(CycleEventKind.POLICY_GATEWAY_APPLIED)
    assert len(applied) == 1
    payload = applied[0].payload
    assert payload["action"] == "defer"
    assert payload["violation_count"] == len(fa.violations)


@pytest.mark.asyncio
async def test_reject_decision_passes_through_with_zero_violations() -> None:
    """The gateway is a no-op for non-ADOPT decisions but still emits an
    event so the cycle log has a uniform "decided + projected" trace."""
    log = EventLog()
    decision = _strategy_decision(
        backtest_run_id=_meta(agent="backtest").run_id,
        action=DecisionAction.REJECT,
    )
    cfg = PolicyGatewayConfig(account_notional_cap_usd=Decimal("100"))
    runner = _make_runner(decision=decision, policy_gateway_config=cfg, event_log=log)
    result = await runner.run(_observer_input())

    fa = result.artifacts.feasible_action
    assert isinstance(fa, FeasibleAction)
    assert fa.action is DecisionAction.REJECT
    assert fa.targets == ()
    assert fa.violations == ()

    applied = log.filter_by_kind(CycleEventKind.POLICY_GATEWAY_APPLIED)
    assert len(applied) == 1
    assert applied[0].payload["action"] == "reject"
    assert applied[0].payload["violation_count"] == 0


@pytest.mark.asyncio
async def test_audit_fail_skips_gateway_too() -> None:
    """If the cycle terminates before the Decider runs (audit fail), the
    gateway is not invoked — there's no decision to project."""
    log = EventLog()

    obs = _observer_artifact()
    hyp = _hypothesis_card()
    sk = _skeptic_report(hypothesis_run_id=hyp.metadata.run_id)
    rp = _research_plan(hypothesis_run_id=hyp.metadata.run_id)
    backtest_run_id = _meta(agent="backtest").run_id
    failing_audit = AuditReport(
        metadata=_meta(agent="auditor"),
        hypothesis_run_id=hyp.metadata.run_id,
        backtest_run_id=backtest_run_id,
        verdict=AuditVerdict.FAIL,
        checks=(
            AcceptanceCheck(
                metric_path="aggregate.median_sharpe",
                op=">",
                threshold=Decimal("0.5"),
                actual=Decimal("0.1"),
                passed=False,
            ),
        ),
        rationale="below threshold",
    )

    async def bt_executor(plan: ResearchPlan) -> BacktestReport:
        return _backtest_report(plan_run_id=plan.metadata.run_id)

    decision = _strategy_decision(backtest_run_id=backtest_run_id)
    runner = CycleRunner(
        observer=_StubAgent(name="observer", result=_ok_result(obs, agent="observer")),
        hypothesis=_StubAgent(name="hypothesis", result=_ok_result(hyp, agent="hypothesis")),
        skeptic=_StubAgent(name="skeptic", result=_ok_result(sk, agent="skeptic")),
        research=_StubAgent(name="research", result=_ok_result(rp, agent="research")),
        auditor=_StubAgent(name="auditor", result=_ok_result(failing_audit, agent="auditor")),
        decider=_StubAgent(name="decider", result=_ok_result(decision, agent="decider")),
        backtest_executor=bt_executor,
        event_log=log,
        budget=CycleBudget(
            cycle_id=new_cycle_id(),
            token_cap=10_000,
            wallclock_seconds_cap=60.0,
        ),
        policy_gateway_config=PolicyGatewayConfig(),
    )
    result = await runner.run(_observer_input())

    assert result.artifacts.decision is None
    assert result.artifacts.feasible_action is None
    applied = log.filter_by_kind(CycleEventKind.POLICY_GATEWAY_APPLIED)
    assert applied == ()
