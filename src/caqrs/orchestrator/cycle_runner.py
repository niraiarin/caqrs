"""End-to-end research-cycle driver.

The :class:`CycleRunner` composes the orchestration primitives
(state machine + event log + budget guard) with the five typed
agents (observer / hypothesis / skeptic / research / auditor) into
one async ``run(observer_input) -> CycleResult`` entry point.

The backtest step is executed by an injected
:data:`BacktestExecutor` callable (``ResearchPlan ->
Awaitable[BacktestReport]``). P1 ships with a stub-friendly contract
so cycle-runner tests never touch a real backtest engine; the
production engine arrives in P2.

Termination semantics:

- **Happy path:** Observer → Hypothesis → Skeptic (verdict=PROCEED)
  → Research → backtest → Auditor. Terminal state ``AUDITING``;
  ``CYCLE_COMPLETED`` event with aggregate token totals.
- **Skeptic non-proceed:** Verdict ``KILL`` or ``REQUIRE_REVISION``
  ends the cycle cleanly at terminal state ``SCRUTINIZING``;
  ``CYCLE_COMPLETED`` (a kill is a normal outcome, not an abort).
- **Agent failure** (``AgentResult.error`` set): emits
  ``AGENT_FAILED`` + ``CYCLE_ABORTED``, transitions to ``ERROR``,
  returns with ``aborted_reason`` populated.
- **Budget breach** (token or wallclock): the BudgetGuard emits
  ``BUDGET_EXCEEDED`` itself; the runner additionally emits
  ``CYCLE_ABORTED``, transitions to ``ERROR``, and returns.

The loop detector and preflight scanners are deliberately *not*
wired into the runner — they operate inside an LLM-tool-call session
and so live at the agent / provider layer, not at this orchestration
layer.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from caqrs.agents.auditor import AuditorInput
from caqrs.agents.protocol import Agent, AgentResult
from caqrs.agents.research import ResearchInput
from caqrs.orchestrator.budget import (
    BudgetGuard,
    BudgetStatus,
    BudgetStatusKind,
    CycleBudget,
)
from caqrs.orchestrator.event_log import EventLog
from caqrs.orchestrator.events import (
    agent_failed_event,
    agent_invoked_event,
    agent_succeeded_event,
    cycle_aborted_event,
    cycle_completed_event,
    cycle_started_event,
    state_transition_event,
)
from caqrs.orchestrator.state import OrchestratorState
from caqrs.orchestrator.state_machine import (
    OrchestratorStateMachine,
    StateTransition,
)
from caqrs.schemas.audit import AuditReport
from caqrs.schemas.backtest_report import BacktestReport
from caqrs.schemas.common import new_run_id
from caqrs.schemas.hypothesis_card import HypothesisCard
from caqrs.schemas.observer import ObserverArtifact, ObserverInput
from caqrs.schemas.research_plan import ResearchPlan
from caqrs.schemas.skeptic import SkepticReport, SkepticVerdict

BacktestExecutor = Callable[[ResearchPlan], Awaitable[BacktestReport]]


class CycleArtifacts(BaseModel):
    """Artifacts produced during a single cycle run.

    Each slot is populated as the corresponding agent succeeds; an
    early-terminating cycle leaves later slots as ``None``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    observer: ObserverArtifact | None = None
    hypothesis: HypothesisCard | None = None
    skeptic: SkepticReport | None = None
    research: ResearchPlan | None = None
    backtest: BacktestReport | None = None
    audit: AuditReport | None = None


class CycleResult(BaseModel):
    """Outcome of one :meth:`CycleRunner.run` invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    cycle_id: str
    terminal_state: OrchestratorState
    artifacts: CycleArtifacts
    aborted_reason: str | None = None
    total_token_in: int = 0
    total_token_out: int = 0


class _HaltKind(StrEnum):
    AGENT_FAILED = "agent_failed"
    BUDGET_EXCEEDED = "budget_exceeded"


@dataclass(frozen=True)
class _Halt:
    kind: _HaltKind
    reason: str


@dataclass
class _ArtifactsBuilder:
    observer: ObserverArtifact | None = None
    hypothesis: HypothesisCard | None = None
    skeptic: SkepticReport | None = None
    research: ResearchPlan | None = None
    backtest: BacktestReport | None = None
    audit: AuditReport | None = None

    def count(self) -> int:
        return sum(
            1
            for v in (
                self.observer,
                self.hypothesis,
                self.skeptic,
                self.research,
                self.backtest,
                self.audit,
            )
            if v is not None
        )

    def freeze(self) -> CycleArtifacts:
        return CycleArtifacts(
            observer=self.observer,
            hypothesis=self.hypothesis,
            skeptic=self.skeptic,
            research=self.research,
            backtest=self.backtest,
            audit=self.audit,
        )


@dataclass
class _TokenTotals:
    token_in: int = 0
    token_out: int = 0


@dataclass
class _RunContext:
    cycle_id: str
    machine: OrchestratorStateMachine
    guard: BudgetGuard
    artifacts: _ArtifactsBuilder
    totals: _TokenTotals
    event_log: EventLog


class CycleRunner:
    """Drives one research cycle from Observer through Auditor."""

    def __init__(
        self,
        *,
        observer: Agent[ObserverInput, ObserverArtifact],
        hypothesis: Agent[ObserverArtifact, HypothesisCard],
        skeptic: Agent[HypothesisCard, SkepticReport],
        research: Agent[ResearchInput, ResearchPlan],
        auditor: Agent[AuditorInput, AuditReport],
        backtest_executor: BacktestExecutor,
        event_log: EventLog,
        budget: CycleBudget,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._observer = observer
        self._hypothesis = hypothesis
        self._skeptic = skeptic
        self._research = research
        self._auditor = auditor
        self._backtest_executor = backtest_executor
        self._event_log = event_log
        self._budget = budget
        self._clock = clock

    async def run(self, observer_input: ObserverInput) -> CycleResult:  # noqa: PLR0911
        # Each early-return is a deliberate fail-fast at a phase boundary
        # (observer / hypothesis / skeptic / research / backtest / auditor).
        # Collapsing them into a loop would obscure the typed contract that
        # each phase consumes the previous artifact.
        cycle_id = self._budget.cycle_id
        state_machine = OrchestratorStateMachine()
        guard = BudgetGuard(budget=self._budget, event_log=self._event_log, clock=self._clock)
        state_machine.on_transition(
            lambda t: self._emit_state_transition(cycle_id=cycle_id, transition=t),
        )

        ctx = _RunContext(
            cycle_id=cycle_id,
            machine=state_machine,
            guard=guard,
            artifacts=_ArtifactsBuilder(),
            totals=_TokenTotals(),
            event_log=self._event_log,
        )

        self._event_log.append(cycle_started_event(cycle_id=cycle_id))
        ctx.machine.transition(OrchestratorState.OBSERVING)

        # === Observer ===
        observer_outcome = await self._call_agent(
            ctx=ctx,
            agent=self._observer,
            agent_name="observer",
            payload=observer_input,
        )
        if isinstance(observer_outcome, _Halt):
            return self._finalize(ctx=ctx, halt=observer_outcome)
        ctx.artifacts.observer = observer_outcome

        ctx.machine.transition(OrchestratorState.HYPOTHESIZING)

        # === Hypothesis ===
        hypothesis_outcome = await self._call_agent(
            ctx=ctx,
            agent=self._hypothesis,
            agent_name="hypothesis",
            payload=observer_outcome,
        )
        if isinstance(hypothesis_outcome, _Halt):
            return self._finalize(ctx=ctx, halt=hypothesis_outcome)
        ctx.artifacts.hypothesis = hypothesis_outcome

        ctx.machine.transition(OrchestratorState.SCRUTINIZING)

        # === Skeptic ===
        skeptic_outcome = await self._call_agent(
            ctx=ctx,
            agent=self._skeptic,
            agent_name="skeptic",
            payload=hypothesis_outcome,
        )
        if isinstance(skeptic_outcome, _Halt):
            return self._finalize(ctx=ctx, halt=skeptic_outcome)
        ctx.artifacts.skeptic = skeptic_outcome

        if skeptic_outcome.verdict is not SkepticVerdict.PROCEED:
            return self._complete(ctx=ctx, terminal=OrchestratorState.SCRUTINIZING)

        ctx.machine.transition(OrchestratorState.RESEARCHING)

        # === Research ===
        research_outcome = await self._call_agent(
            ctx=ctx,
            agent=self._research,
            agent_name="research",
            payload=ResearchInput(hypothesis=hypothesis_outcome, skeptic=skeptic_outcome),
        )
        if isinstance(research_outcome, _Halt):
            return self._finalize(ctx=ctx, halt=research_outcome)
        ctx.artifacts.research = research_outcome

        # === Backtest (executor; not a CAQRS Agent) ===
        try:
            backtest_report = await self._backtest_executor(research_outcome)
        except Exception as exc:
            return self._finalize(
                ctx=ctx,
                halt=_Halt(
                    kind=_HaltKind.AGENT_FAILED,
                    reason=f"backtest_executor: {exc}",
                ),
            )
        ctx.artifacts.backtest = backtest_report

        ctx.machine.transition(OrchestratorState.AUDITING)

        # === Auditor ===
        audit_outcome = await self._call_agent(
            ctx=ctx,
            agent=self._auditor,
            agent_name="auditor",
            payload=AuditorInput(hypothesis=hypothesis_outcome, backtest=backtest_report),
        )
        if isinstance(audit_outcome, _Halt):
            return self._finalize(ctx=ctx, halt=audit_outcome)
        ctx.artifacts.audit = audit_outcome

        return self._complete(ctx=ctx, terminal=OrchestratorState.AUDITING)

    # === Helpers ===

    async def _call_agent[I: BaseModel, O: BaseModel](
        self,
        *,
        ctx: _RunContext,
        agent: Agent[I, O],
        agent_name: str,
        payload: I,
    ) -> O | _Halt:
        run_id = new_run_id()
        ctx.event_log.append(
            agent_invoked_event(cycle_id=ctx.cycle_id, agent_name=agent_name, run_id=run_id),
        )
        agent_result: AgentResult[O] = await agent.run(payload)

        if not agent_result.is_ok():
            ctx.event_log.append(
                agent_failed_event(
                    cycle_id=ctx.cycle_id,
                    agent_name=agent_name,
                    run_id=run_id,
                    error=agent_result.error or "unknown error",
                ),
            )
            return _Halt(
                kind=_HaltKind.AGENT_FAILED,
                reason=f"{agent_name}: {agent_result.error or 'unknown error'}",
            )

        meta = agent_result.metadata
        output = agent_result.output
        assert output is not None  # is_ok() guarantees this

        ctx.event_log.append(
            agent_succeeded_event(
                cycle_id=ctx.cycle_id,
                agent_name=agent_name,
                run_id=run_id,
                output_schema=type(output).__name__,
                token_in=meta.token_in,
                token_out=meta.token_out,
                latency_ms=meta.latency_ms,
            ),
        )

        ctx.totals.token_in += meta.token_in
        ctx.totals.token_out += meta.token_out

        status: BudgetStatus = ctx.guard.consume(
            token_in=meta.token_in,
            token_out=meta.token_out,
        )
        if status.kind is not BudgetStatusKind.OK:
            return _Halt(
                kind=_HaltKind.BUDGET_EXCEEDED,
                reason=f"budget {status.kind.value}: tokens={status.tokens_consumed}, "
                f"elapsed={status.elapsed_seconds:.1f}s",
            )

        return output

    def _emit_state_transition(self, *, cycle_id: str, transition: StateTransition) -> None:
        self._event_log.append(
            state_transition_event(
                cycle_id=cycle_id,
                src=transition.src.value,
                dst=transition.dst.value,
            ),
        )

    def _complete(self, *, ctx: _RunContext, terminal: OrchestratorState) -> CycleResult:
        # Wind the machine back to IDLE so the runner-managed machine is
        # in a well-defined resting state for downstream observers; the
        # terminal_state recorded in CycleResult preserves the agent that
        # actually ended the cycle.
        ctx.machine.transition(OrchestratorState.IDLE)
        self._event_log.append(
            cycle_completed_event(
                cycle_id=ctx.cycle_id,
                terminal_state=terminal.value,
                artifacts_emitted=ctx.artifacts.count(),
                total_token_in=ctx.totals.token_in,
                total_token_out=ctx.totals.token_out,
            ),
        )
        return CycleResult(
            cycle_id=ctx.cycle_id,
            terminal_state=terminal,
            artifacts=ctx.artifacts.freeze(),
            aborted_reason=None,
            total_token_in=ctx.totals.token_in,
            total_token_out=ctx.totals.token_out,
        )

    def _finalize(self, *, ctx: _RunContext, halt: _Halt) -> CycleResult:
        at_state = ctx.machine.state.value
        ctx.machine.transition(OrchestratorState.ERROR)
        self._event_log.append(
            cycle_aborted_event(
                cycle_id=ctx.cycle_id,
                reason=halt.reason,
                at_state=at_state,
            ),
        )
        return CycleResult(
            cycle_id=ctx.cycle_id,
            terminal_state=OrchestratorState.ERROR,
            artifacts=ctx.artifacts.freeze(),
            aborted_reason=halt.reason,
            total_token_in=ctx.totals.token_in,
            total_token_out=ctx.totals.token_out,
        )
