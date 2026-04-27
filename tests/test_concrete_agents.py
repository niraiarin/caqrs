"""Tests for the five concrete agents.

For each agent we verify:
- ``emit_tool_name`` derives from the output-schema class name
- ``system_prompt`` contains the role brief and the emit tool name
- ``run`` succeeds against a fake provider and returns the configured output
- input validation rejects mismatched types

Cross-agent: the chain is plumbing-level checked (each agent's output
type matches the next agent's input element where applicable).
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from caqrs.agents import (
    AuditorAgent,
    AuditorInput,
    HypothesisAgent,
    ObserverAgent,
    ResearchAgent,
    ResearchInput,
    SkepticAgent,
)
from caqrs.providers import CompletionResult, Message, ProviderUsage
from caqrs.schemas import (
    AcceptanceCheck,
    AcceptanceCriterion,
    AggregateMetrics,
    AuditReport,
    AuditVerdict,
    BacktestReport,
    DataDimension,
    DataFrequency,
    Direction,
    FalsificationPath,
    FoldMetrics,
    HypothesisCard,
    HypothesisStatus,
    ObserverArtifact,
    ObserverInput,
    ResearchPlan,
    Severity,
    SkepticReport,
    SkepticVerdict,
    WalkForwardWindow,
    new_run_id,
    utc_now,
)
from caqrs.schemas.common import RunMetadata

# === Fake provider ===


class _RecordingProvider:
    provider_id: str = "fake-provider/test-model"

    def __init__(self, *, respond_with: BaseModel) -> None:
        self._respond_with = respond_with

    async def complete[T: BaseModel](
        self,
        *,
        messages: tuple[Message, ...],
        schema: type[T],
        max_output_tokens: int,
        temperature: float = 0.0,
    ) -> CompletionResult[T]:
        del messages, max_output_tokens, temperature
        if not isinstance(self._respond_with, schema):
            raise AssertionError(
                f"fake provider configured with {type(self._respond_with).__name__} "
                f"but agent requested {schema.__name__}",
            )
        return CompletionResult[T](
            output=self._respond_with,
            usage=ProviderUsage(token_in=1, token_out=1, latency_ms=1, cost_usd=Decimal(0)),
            provider_id=self.provider_id,
        )


# === Fixture builders ===


def _meta(agent: str) -> RunMetadata:
    return RunMetadata(
        run_id=new_run_id(),
        parent_id=None,
        agent_name=agent,
        model_id="test",
        created_at=utc_now(),
    )


def _observer_input() -> ObserverInput:
    return ObserverInput(
        universe=("AAPL", "MSFT"),
        as_of=utc_now(),
        horizon_days=30,
        dimensions=(DataDimension.PRICES, DataDimension.NEWS),
    )


def _observer_artifact() -> ObserverArtifact:
    now = utc_now()
    return ObserverArtifact(
        metadata=_meta("observer"),
        universe=("AAPL", "MSFT"),
        as_of=now,
        regime_summary="Low-vol bullish; SPX +2% MTD.",
    )


def _hypothesis_card() -> HypothesisCard:
    now = utc_now()
    return HypothesisCard(
        metadata=_meta("hypothesis"),
        status=HypothesisStatus.DRAFT,
        claim="Momentum on US large-caps outperforms equal-weight in low-vol regimes.",
        rationale="Heuristic from Jegadeesh & Titman; skeptic to challenge.",
        universe=("AAPL", "MSFT"),
        direction=Direction.LONG,
        horizon_days=30,
        variables=("12_1_momentum",),
        acceptance=(
            AcceptanceCriterion(
                metric_path="aggregate.median_sharpe",
                op=">=",
                threshold=Decimal("0.5"),
            ),
        ),
        max_drawdown_pct=Decimal("20"),
        expected_window_start=now,
        expected_window_end=now + timedelta(days=30),
    )


def _skeptic_report(hypothesis_run_id: str) -> SkepticReport:
    return SkepticReport(
        metadata=_meta("skeptic"),
        hypothesis_run_id=hypothesis_run_id,
        verdict=SkepticVerdict.PROCEED,
        falsification_paths=(
            FalsificationPath(
                description="May break in high-vol.",
                severity=Severity.MEDIUM,
                evidence_marker="median_sharpe < 0 when VIX > 30",
            ),
        ),
        summary="No fatal flaws.",
    )


def _research_plan(hypothesis_run_id: str) -> ResearchPlan:
    now = utc_now()
    return ResearchPlan(
        metadata=_meta("research"),
        hypothesis_run_id=hypothesis_run_id,
        universe=("AAPL", "MSFT"),
        frequency=DataFrequency.DAILY,
        walk_forward=(
            WalkForwardWindow(
                train_start=now,
                train_end=now + timedelta(days=100),
                test_start=now + timedelta(days=100),
                test_end=now + timedelta(days=130),
            ),
        ),
        cost_model_bps=Decimal("3"),
        slippage_bps=Decimal("1"),
        seed=42,
    )


def _backtest_report(plan_run_id: str) -> BacktestReport:
    now = utc_now()
    return BacktestReport(
        metadata=_meta("research"),
        plan_run_id=plan_run_id,
        folds=(
            FoldMetrics(
                fold_index=0,
                test_start=now,
                test_end=now + timedelta(days=30),
                sharpe=Decimal("0.7"),
                max_drawdown_pct=Decimal("10"),
                turnover=Decimal("1"),
                n_trades=12,
                pnl_usd=Decimal("1234"),
            ),
        ),
        aggregate=AggregateMetrics(
            median_sharpe=Decimal("0.7"),
            mean_sharpe=Decimal("0.7"),
            worst_fold_sharpe=Decimal("0.7"),
            median_max_drawdown_pct=Decimal("10"),
            total_pnl_usd=Decimal("1234"),
            total_trades=12,
        ),
    )


def _audit_report(hypothesis_run_id: str, backtest_run_id: str) -> AuditReport:
    return AuditReport(
        metadata=_meta("auditor"),
        hypothesis_run_id=hypothesis_run_id,
        backtest_run_id=backtest_run_id,
        verdict=AuditVerdict.PASS,
        checks=(
            AcceptanceCheck(
                metric_path="aggregate.median_sharpe",
                op=">=",
                threshold=Decimal("0.5"),
                actual=Decimal("0.7"),
                passed=True,
            ),
        ),
        rationale="All criteria cleared.",
    )


# === ObserverAgent ===


def test_observer_emit_tool_name() -> None:
    agent = ObserverAgent(provider=_RecordingProvider(respond_with=_observer_artifact()))
    assert agent.emit_tool_name == "emit_ObserverArtifact"


def test_observer_system_prompt_contains_role_and_emit() -> None:
    agent = ObserverAgent(provider=_RecordingProvider(respond_with=_observer_artifact()))
    prompt = agent.system_prompt
    assert "observer agent" in prompt
    assert "emit_ObserverArtifact" in prompt
    assert "RESEARCH GUARDRAILS" in prompt


async def test_observer_run_succeeds() -> None:
    output = _observer_artifact()
    agent = ObserverAgent(provider=_RecordingProvider(respond_with=output))
    result = await agent.run(_observer_input())
    assert result.is_ok()
    assert result.output is output


async def test_observer_run_rejects_wrong_input() -> None:
    agent = ObserverAgent(provider=_RecordingProvider(respond_with=_observer_artifact()))
    with pytest.raises(TypeError):
        await agent.run(_observer_artifact())  # type: ignore[arg-type]


# === HypothesisAgent ===


def test_hypothesis_emit_tool_name() -> None:
    agent = HypothesisAgent(provider=_RecordingProvider(respond_with=_hypothesis_card()))
    assert agent.emit_tool_name == "emit_HypothesisCard"


async def test_hypothesis_run_succeeds() -> None:
    output = _hypothesis_card()
    agent = HypothesisAgent(provider=_RecordingProvider(respond_with=output))
    result = await agent.run(_observer_artifact())
    assert result.is_ok()
    assert result.output is output


# === SkepticAgent ===


def test_skeptic_emit_tool_name() -> None:
    card = _hypothesis_card()
    agent = SkepticAgent(
        provider=_RecordingProvider(respond_with=_skeptic_report(card.metadata.run_id)),
    )
    assert agent.emit_tool_name == "emit_SkepticReport"


async def test_skeptic_run_succeeds() -> None:
    card = _hypothesis_card()
    output = _skeptic_report(card.metadata.run_id)
    agent = SkepticAgent(provider=_RecordingProvider(respond_with=output))
    result = await agent.run(card)
    assert result.is_ok()
    assert result.output is output


# === ResearchAgent ===


def test_research_emit_tool_name() -> None:
    card = _hypothesis_card()
    agent = ResearchAgent(
        provider=_RecordingProvider(respond_with=_research_plan(card.metadata.run_id)),
    )
    assert agent.emit_tool_name == "emit_ResearchPlan"


async def test_research_run_succeeds() -> None:
    card = _hypothesis_card()
    output = _research_plan(card.metadata.run_id)
    agent = ResearchAgent(provider=_RecordingProvider(respond_with=output))
    payload = ResearchInput(hypothesis=card, skeptic=_skeptic_report(card.metadata.run_id))
    result = await agent.run(payload)
    assert result.is_ok()
    assert result.output is output


def test_research_input_is_frozen() -> None:
    card = _hypothesis_card()
    payload = ResearchInput(hypothesis=card, skeptic=_skeptic_report(card.metadata.run_id))
    with pytest.raises(ValueError, match="frozen"):
        payload.hypothesis = card  # type: ignore[misc]


def test_research_input_rejects_extras() -> None:
    card = _hypothesis_card()
    with pytest.raises(ValidationError):
        ResearchInput(  # type: ignore[call-arg]
            hypothesis=card,
            skeptic=_skeptic_report(card.metadata.run_id),
            extra="nope",
        )


# === AuditorAgent ===


def test_auditor_emit_tool_name() -> None:
    card = _hypothesis_card()
    plan = _research_plan(card.metadata.run_id)
    agent = AuditorAgent(
        provider=_RecordingProvider(
            respond_with=_audit_report(card.metadata.run_id, plan.metadata.run_id),
        ),
    )
    assert agent.emit_tool_name == "emit_AuditReport"


async def test_auditor_run_succeeds() -> None:
    card = _hypothesis_card()
    plan = _research_plan(card.metadata.run_id)
    backtest = _backtest_report(plan.metadata.run_id)
    output = _audit_report(card.metadata.run_id, backtest.metadata.run_id)
    agent = AuditorAgent(provider=_RecordingProvider(respond_with=output))
    payload = AuditorInput(hypothesis=card, backtest=backtest)
    result = await agent.run(payload)
    assert result.is_ok()
    assert result.output is output


def test_auditor_input_is_frozen() -> None:
    card = _hypothesis_card()
    plan = _research_plan(card.metadata.run_id)
    backtest = _backtest_report(plan.metadata.run_id)
    payload = AuditorInput(hypothesis=card, backtest=backtest)
    with pytest.raises(ValueError, match="frozen"):
        payload.backtest = backtest  # type: ignore[misc]


# === Cross-agent plumbing ===


def test_pipeline_input_output_chain_compiles() -> None:
    """The five agents type-chain end-to-end. This is a compile-only check
    using ``isinstance`` to confirm output types match downstream input
    schemas without actually running the pipeline."""
    obs = ObserverAgent(provider=_RecordingProvider(respond_with=_observer_artifact()))
    hyp = HypothesisAgent(provider=_RecordingProvider(respond_with=_hypothesis_card()))
    skp = SkepticAgent(
        provider=_RecordingProvider(
            respond_with=_skeptic_report(_hypothesis_card().metadata.run_id),
        ),
    )
    rsh = ResearchAgent(
        provider=_RecordingProvider(
            respond_with=_research_plan(_hypothesis_card().metadata.run_id),
        ),
    )
    aud = AuditorAgent(
        provider=_RecordingProvider(
            respond_with=_audit_report(
                _hypothesis_card().metadata.run_id,
                _backtest_report("a" * 16).metadata.run_id,
            ),
        ),
    )
    # Output schemas wire into next-stage input schemas
    assert obs.output_schema is hyp.input_schema  # ObserverArtifact
    assert hyp.output_schema is skp.input_schema  # HypothesisCard
    # ResearchAgent / AuditorAgent take composite inputs (not directly chainable)
    assert rsh.input_schema is ResearchInput
    assert aud.input_schema is AuditorInput
