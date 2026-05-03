"""Parametrized broker contract suite — NFR-LIVE-BROKER-1..7.

Asserts the seven non-functional safety requirements from
:doc:`docs/decisions/0008-live-broker-safety` against any concrete
``BrokerProtocol`` implementation. The fixture is parametrized so the
same suite runs against PaperBroker today and against LiveBroker once
P4 lands; LiveBroker's slot is intentionally left as a TODO comment in
the fixture.

Two-step TDD dispatch (ADR-0006):

- **Step 1** (commit ``455251e``): every NFR test was decorated
  ``@pytest.mark.xfail(strict=True)`` with a body raising
  ``NotImplementedError``, so ``pytest -q`` reported ``8 xfailed`` —
  the red phase the verifier audits.
- **Step 2** (this commit): tests for the NFRs that PaperBroker
  actually satisfies (NFR-1 default-off, NFR-2 credential isolation,
  NFR-7 distinct event taxonomy) get real assertion bodies and have
  ``xfail`` removed. Tests that genuinely require a real LiveBroker
  (NFR-3, -4, -5, -6) keep ``xfail`` with a documented reason; the
  LiveBroker PR in P4 will flip them to passing.

Conventions:

- Tests are channel-agnostic: no stdout, no readline. They construct
  a broker through the fixture and exercise its public surface.
- Static checks (NFR-2) run against the implementation module via
  ``inspect.getsource`` — no I/O, fast, fully deterministic.
- Behavioural checks (NFR-7) construct a one-shot CycleRunner with a
  real PaperBroker and assert against the EventLog. This is
  duplicative of ``test_orchestrator_paper_broker.py`` but scoped to
  the *taxonomy* check rather than wiring.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel

from caqrs.agents.protocol import AgentResult
from caqrs.execution import paper_broker as paper_broker_module
from caqrs.execution.execution_report import ExecutionStatus
from caqrs.execution.paper_broker import PaperBroker
from caqrs.orchestrator import (
    CycleBudget,
    CycleEventKind,
    CycleRunner,
    EventLog,
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

if TYPE_CHECKING:
    from caqrs.execution.protocol import BrokerProtocol


# === Fixture: parametrize across broker implementations =====================


@pytest.fixture(
    params=[
        pytest.param(
            lambda: PaperBroker(initial_capital_usd=Decimal("100000")),
            id="PaperBroker",
        ),
        # LiveBroker will be added in P4 PR; the contract suite already
        # has the slot. Adding the param is the only test-side change
        # required when LiveBroker lands.
    ],
)
def broker(request: pytest.FixtureRequest) -> BrokerProtocol:
    factory: Callable[[], BrokerProtocol] = request.param
    return factory()


# === NFR-LIVE-BROKER-1: Default-off ========================================
# Spec: docs/decisions/0008-live-broker-safety.md §"NFR-LIVE-BROKER-1"
# PaperBroker reading: "is not a live broker; doesn't claim to be" —
# verified by the absence of an ``enable_live_orders`` attribute and
# any other live-order surface markers. A LiveBroker implementation
# MUST expose ``enable_live_orders`` and default it to False.


def test_default_off_for_live_brokers_only(broker: BrokerProtocol) -> None:
    """A live broker MUST expose ``enable_live_orders`` defaulting to
    ``False``; a paper broker MUST NOT claim to be a live broker.

    Both shapes are valid contract states; the contract is that **one
    or the other** is true, not silently ambiguous (no live broker that
    forgot the flag, no paper broker that pretends to be live).
    """
    has_flag = hasattr(broker, "enable_live_orders")
    if has_flag:
        # LiveBroker side of the contract: must default to False.
        flag_value = broker.enable_live_orders  # type: ignore[attr-defined]
        assert flag_value is False, (
            f"LiveBroker.enable_live_orders MUST default to False; got {flag_value!r}"
        )
    else:
        # Paper-broker side of the contract: must not expose any
        # live-order surface that could be misread as "live mode".
        for live_marker in (
            "submit_live_order",
            "live_session",
            "live_account_id",
            "broker_live_url",
            "kill_switch",
        ):
            assert not hasattr(broker, live_marker), (
                f"PaperBroker MUST NOT expose live-order surface "
                f"{live_marker!r} (NFR-LIVE-BROKER-1: default-off "
                "applies only to brokers that *can* go live)"
            )


# === NFR-LIVE-BROKER-2: Credential isolation ===============================
# Spec: docs/decisions/0008-live-broker-safety.md §"NFR-LIVE-BROKER-2"
# Static-check approach: a broker MUST NOT reference env vars belonging
# to a different broker family. PaperBroker reads no env vars at all
# (in-memory only); LiveBroker will read LIVE_BROKER_* but never
# JQUANTS_*, EDINET_*, etc.


_FOREIGN_ENV_PREFIXES_FOR_PAPER = ("LIVE_BROKER_",)
_FOREIGN_ENV_PREFIXES_FOR_LIVE = (
    "JQUANTS_",
    "EDINET_",
    "POLYMARKET_",
    "YFINANCE_",
)


def test_paper_broker_does_not_import_live_broker_env_vars() -> None:
    """Static-import audit: PaperBroker source MUST NOT mention any
    ``LIVE_BROKER_*`` env var. A future LiveBroker reads
    ``LIVE_BROKER_*`` creds; if they leak into the paper code path, the
    credential-isolation perimeter is broken.

    Implementation: source inspection of the ``paper_broker`` module.
    Any ``LIVE_BROKER_*`` token in the source — string literal,
    comment, docstring — fails the test. The intent is conservative:
    even a docstring mention is suspicious enough to surface for
    review.
    """
    src = inspect.getsource(paper_broker_module)
    for prefix in _FOREIGN_ENV_PREFIXES_FOR_PAPER:
        assert prefix not in src, (
            f"PaperBroker source contains foreign env-var prefix "
            f"{prefix!r}; NFR-LIVE-BROKER-2 forbids credential leakage "
            "across broker classes"
        )


def test_broker_does_not_leak_credentials_across_classes(
    broker: BrokerProtocol,
) -> None:
    """Generalisation of the above: whichever broker is under test,
    its source MUST NOT reference env vars belonging to a different
    broker family.

    The "different family" is decided dynamically: a broker that
    exposes ``enable_live_orders`` is treated as a live broker
    (foreign prefixes = data-source vars like ``JQUANTS_``,
    ``EDINET_``); a broker that does not is treated as paper
    (foreign prefix = ``LIVE_BROKER_``).
    """
    is_live = hasattr(broker, "enable_live_orders")
    foreign = _FOREIGN_ENV_PREFIXES_FOR_LIVE if is_live else _FOREIGN_ENV_PREFIXES_FOR_PAPER
    module = inspect.getmodule(type(broker)) or paper_broker_module
    src = inspect.getsource(module)
    for prefix in foreign:
        assert prefix not in src, (
            f"{type(broker).__name__} source contains foreign env-var "
            f"prefix {prefix!r}; NFR-LIVE-BROKER-2 forbids credential "
            "leakage"
        )


# === NFR-LIVE-BROKER-3: Dry-run parity =====================================
# Spec: docs/decisions/0008-live-broker-safety.md §"NFR-LIVE-BROKER-3"


@pytest.mark.xfail(
    strict=True,
    reason="PaperBroker has no separate dry-run mode (it IS the dry-run); "
    "NFR-LIVE-BROKER-3 applies to LiveBroker pre-flight wiring",
)
@pytest.mark.asyncio
async def test_dry_run_does_not_change_broker_state(broker: BrokerProtocol) -> None:
    """LiveBroker MUST expose a ``dry_run=True`` execute path that
    invokes ``PaperBroker.execute`` internally and asserts the result
    is ``ExecutionStatus.FILLED`` before any venue submission. State
    on the live broker (positions, idempotency-key log, kill-switch
    counter) MUST be unchanged after a dry-run call.

    For PaperBroker this NFR is N/A — there is no separate dry-run
    mode because the entire broker IS the dry-run. The xfail here
    documents the contract for LiveBroker; the assertion will be
    authored when LiveBroker lands.
    """
    raise NotImplementedError("LiveBroker dry-run-parity assertion deferred to P4 PR")


# === NFR-LIVE-BROKER-4: Idempotency key on every order =====================
# Spec: docs/decisions/0008-live-broker-safety.md §"NFR-LIVE-BROKER-4"


@pytest.mark.xfail(
    strict=True,
    reason="PaperBroker doesn't implement idempotency keys; "
    "NFR-LIVE-BROKER-4 is a LiveBroker contract",
)
def test_idempotency_key_is_deterministic(broker: BrokerProtocol) -> None:
    """LiveBroker MUST expose a ``compute_idempotency_key`` helper
    such that for any
    ``(cycle_id, decision_run_id, ticker, side, quantity)`` tuple,
    repeated invocations return the same sha256 hex digest.

    Spec key derivation:
    ``sha256_hex((cycle_id, decision_run_id, ticker, side, quantity))``
    (canonical-JSON of the same fields is also acceptable; the exact
    serialization is a P4 ADR-0009 detail).

    For PaperBroker this NFR is N/A — paper has no venue, no replay
    semantics, no idempotency contract. The xfail documents the
    expected helper signature; the assertion below describes what the
    LiveBroker PR must satisfy.
    """
    cycle_id = new_cycle_id()
    decision_run_id = new_run_id()
    args = {
        "cycle_id": cycle_id,
        "decision_run_id": decision_run_id,
        "ticker": "AAPL",
        "side": Side.BUY,
        "quantity": Decimal("100"),
    }
    compute = getattr(broker, "compute_idempotency_key", None)
    assert compute is not None, "broker.compute_idempotency_key is required by NFR-LIVE-BROKER-4"
    key_a = compute(**args)
    key_b = compute(**args)
    assert key_a == key_b, "idempotency key MUST be deterministic"
    assert isinstance(key_a, str), "idempotency key MUST be a hex string"
    assert len(key_a) == 64, "idempotency key MUST be a 64-char sha256 hex digest"


# === NFR-LIVE-BROKER-5: Kill-switch ========================================
# Spec: docs/decisions/0008-live-broker-safety.md §"NFR-LIVE-BROKER-5"


@pytest.mark.xfail(
    strict=True,
    reason="PaperBroker is synchronous and has no in-flight order state; "
    "NFR-LIVE-BROKER-5 is a LiveBroker contract",
)
@pytest.mark.asyncio
async def test_kill_switch_aborts_within_one_cycle(broker: BrokerProtocol) -> None:
    """LiveBroker MUST expose a ``kill_switch()`` method that:

    1. Aborts any in-flight orders within ≤ 1 CycleRunner iteration.
    2. Causes subsequent ``execute()`` calls to return
       ``ExecutionStatus.SKIPPED`` with reason ``"kill switch engaged"``
       until a human re-enables via the NFR-1 approval workflow.

    For PaperBroker this NFR is structurally N/A: synchronous,
    in-process execution has no "in-flight" window to abort. The xfail
    documents the expected interface; the assertion body below
    describes what the LiveBroker PR must satisfy.
    """
    kill = getattr(broker, "kill_switch", None)
    assert kill is not None, "broker.kill_switch() is required by NFR-LIVE-BROKER-5"
    kill()
    action = FeasibleAction(
        action=DecisionAction.ADOPT,
        targets=(TargetPosition(ticker="AAPL", side=Side.BUY, weight=Decimal("0.5")),),
        violations=(),
        source_decision_run_id=new_run_id(),
    )
    report = await broker.execute(action=action, prices={"AAPL": Decimal("180")})
    assert report.status is ExecutionStatus.SKIPPED
    assert "kill switch engaged" in (report.reason or "")


# === NFR-LIVE-BROKER-6: Broker-level daily loss cap ========================
# Spec: docs/decisions/0008-live-broker-safety.md §"NFR-LIVE-BROKER-6"


@pytest.mark.xfail(
    strict=True,
    reason="PaperBroker exposes realized_pnl_usd but no cap-trigger; "
    "NFR-LIVE-BROKER-6 is a LiveBroker contract (independent state)",
)
def test_broker_level_daily_loss_cap_independent_from_gateway(
    broker: BrokerProtocol,
) -> None:
    """LiveBroker MUST expose ``live_broker_daily_loss_cap_usd``
    (config) and an internal realized-loss accumulator that triggers
    ``kill_switch()`` (NFR-5) when the cap is breached. The accumulator
    MUST NOT share state with ``PolicyGatewayConfig.daily_loss_limit_usd``
    — the duplicate computation is the safety property (defense in
    depth), not an inefficiency.

    For PaperBroker this NFR is N/A — paper has ``realized_pnl_usd``
    but no cap and no auto-kill-switch. The assertion body below
    describes the expected LiveBroker surface (cap config attribute +
    internal accumulator).
    """
    cap = getattr(broker, "live_broker_daily_loss_cap_usd", None)
    assert cap is not None, "broker.live_broker_daily_loss_cap_usd is required by NFR-LIVE-BROKER-6"
    # Independence assertion: the broker's loss accumulator must be
    # readable WITHOUT any PolicyGatewayConfig in scope.
    accumulator = getattr(broker, "realized_loss_today_usd", None)
    assert accumulator is not None, (
        "broker.realized_loss_today_usd accumulator is required by NFR-LIVE-BROKER-6"
    )


# === NFR-LIVE-BROKER-7: Distinct event taxonomy ============================
# Spec: docs/decisions/0008-live-broker-safety.md §"NFR-LIVE-BROKER-7"
# Live-broker events MUST use BROKER_LIVE_* event kinds; PaperBroker
# MUST keep using BROKER_EXECUTED. This is verifiable today against
# PaperBroker by running a happy-path cycle and asserting the event
# kinds in the EventLog.


# --- helpers for NFR-7 (mirror test_orchestrator_paper_broker.py at
# the minimum required to produce a happy-path BROKER_EXECUTED event) ---


def _meta(*, agent: str = "test") -> RunMetadata:
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
        summary="ok",
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
    return AgentResult[O](output=output, error=None, metadata=_meta(agent=agent))


async def _default_prices(action: FeasibleAction) -> dict[Ticker, Decimal]:
    return {target.ticker: Decimal("180") for target in action.targets}


async def _run_one_cycle_with_paper_broker(*, log: EventLog) -> None:
    """Run one CycleRunner iteration end-to-end with a real PaperBroker
    so the EventLog is populated with a happy-path BROKER_EXECUTED
    event for NFR-7 to assert against."""
    obs = _observer_artifact()
    hyp = _hypothesis_card()
    sk = _skeptic_report(hypothesis_run_id=hyp.metadata.run_id)
    rp = _research_plan(hypothesis_run_id=hyp.metadata.run_id)
    backtest_run_id = _meta(agent="backtest").run_id
    audit = _audit_report(
        hypothesis_run_id=hyp.metadata.run_id,
        backtest_run_id=backtest_run_id,
    )
    decision = _strategy_decision(backtest_run_id=backtest_run_id)

    async def bt_executor(plan: ResearchPlan) -> BacktestReport:
        return _backtest_report(plan_run_id=plan.metadata.run_id)

    runner = CycleRunner(
        observer=_StubAgent(name="observer", result=_ok_result(obs, agent="observer")),
        hypothesis=_StubAgent(name="hypothesis", result=_ok_result(hyp, agent="hypothesis")),
        skeptic=_StubAgent(name="skeptic", result=_ok_result(sk, agent="skeptic")),
        research=_StubAgent(name="research", result=_ok_result(rp, agent="research")),
        auditor=_StubAgent(name="auditor", result=_ok_result(audit, agent="auditor")),
        decider=_StubAgent(name="decider", result=_ok_result(decision, agent="decider")),
        backtest_executor=bt_executor,
        event_log=log,
        budget=CycleBudget(
            cycle_id=new_cycle_id(),
            token_cap=10_000,
            wallclock_seconds_cap=60.0,
        ),
        cycle_store=None,
        policy_gateway_config=PolicyGatewayConfig(),
        broker=PaperBroker(initial_capital_usd=Decimal("10000")),
        price_provider=_default_prices,
    )
    await runner.run(_observer_input())


@pytest.mark.asyncio
async def test_paper_broker_uses_broker_executed_not_broker_live_kinds() -> None:
    """A happy-path PaperBroker cycle MUST emit ``BROKER_EXECUTED`` and
    MUST NOT emit any ``BROKER_LIVE_*`` event kind. NFR-LIVE-BROKER-7
    reserves ``BROKER_LIVE_*`` for the future LiveBroker; if PaperBroker
    starts emitting them, the audit-grade "did this cycle touch real
    money?" grep is broken.

    Implementation: run one cycle, scan ``log.events`` for any kind
    whose value starts with ``broker_live_``. The taxonomy is asserted
    via the StrEnum's *string values* (not just the member names) so
    even a downstream consumer that round-trips through JSON cannot
    accidentally re-introduce a confusing prefix.
    """
    log = EventLog()
    await _run_one_cycle_with_paper_broker(log=log)

    kinds = [event.kind for event in log.events]
    assert CycleEventKind.BROKER_EXECUTED in kinds, (
        "PaperBroker cycle MUST emit BROKER_EXECUTED on the happy path"
    )

    forbidden_values = [k for k in kinds if k.value.startswith("broker_live_")]
    assert forbidden_values == [], (
        f"PaperBroker MUST NOT emit BROKER_LIVE_* event kinds; "
        f"found: {[k.value for k in forbidden_values]}"
    )

    # Defense-in-depth: enum-member-name scan. Even if a future commit
    # adds a BROKER_LIVE_* member, this surfaces it the moment a
    # PaperBroker cycle emits it.
    forbidden_members = [k for k in kinds if k.name.startswith("BROKER_LIVE_")]
    assert forbidden_members == [], (
        f"PaperBroker emitted BROKER_LIVE_* member: {[k.name for k in forbidden_members]}"
    )
