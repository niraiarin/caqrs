"""Orchestrator subsystem: state machine, loop detection, preflight, events.

P1.2.a — ``OrchestratorState`` enum, ``OrchestratorStateMachine``,
``ToolCallLoopDetector`` (ported from Mercury ``src/core/agent.ts`` per
research files 03 + 10).

P1.2.b — preflight warning injection (tool / text repetition scanners),
the pre-LLM half of the dual-loop discipline.

P1.4.a — event types + append-only ``EventLog`` for cycle observability.
Foundation for the subsequent budget / queue / cycle-runner slices.

P1.4.b — per-cycle :class:`BudgetGuard` (token + wallclock caps),
emits ``BUDGET_EXCEEDED`` into the event log on first breach.

P1.4.c — :class:`CycleRunner` composes the primitives above with the
five typed agents into one ``run(observer_input) -> CycleResult``
entry point. Backtest is supplied as an injected callable so P1
ships without the P2 backtest engine.

P1.4.d-mini — :class:`CycleQueue` serial dispatcher with reentrancy
guard, so concurrent enqueues are safe but cycles execute one at a
time.

P1.4.d-full — :class:`Heartbeat` interval-based fire tracker. Pure
object; caller polls ``is_due()`` and calls ``fire()`` after
enqueuing a cycle. No threads, no cron dep — composes with
:class:`CycleQueue` in the caller's event loop.
"""

from caqrs.orchestrator.budget import (
    BudgetGuard,
    BudgetStatus,
    BudgetStatusKind,
    CycleBudget,
)
from caqrs.orchestrator.cycle_queue import CycleQueue
from caqrs.orchestrator.cycle_runner import (
    BacktestExecutor,
    CycleArtifacts,
    CycleResult,
    CycleRunner,
)
from caqrs.orchestrator.event_log import EventLog, load_jsonl
from caqrs.orchestrator.events import (
    CycleEvent,
    CycleEventKind,
    agent_failed_event,
    agent_invoked_event,
    agent_succeeded_event,
    budget_exceeded_event,
    cycle_aborted_event,
    cycle_completed_event,
    cycle_started_event,
    loop_detected_event,
    new_cycle_id,
    new_event_id,
    state_transition_event,
)
from caqrs.orchestrator.heartbeat import Heartbeat
from caqrs.orchestrator.loop_detector import (
    CallRecord,
    LoopDetection,
    ToolCallLoopDetector,
)
from caqrs.orchestrator.preflight import (
    PreflightWarning,
    PreflightWarningKind,
    compose_preflight_message,
    scan_text_repetition,
    scan_tool_repetition,
)
from caqrs.orchestrator.state import OrchestratorState
from caqrs.orchestrator.state_machine import OrchestratorStateMachine, StateTransition

__all__ = [
    "BacktestExecutor",
    "BudgetGuard",
    "BudgetStatus",
    "BudgetStatusKind",
    "CallRecord",
    "CycleArtifacts",
    "CycleBudget",
    "CycleEvent",
    "CycleEventKind",
    "CycleQueue",
    "CycleResult",
    "CycleRunner",
    "EventLog",
    "Heartbeat",
    "LoopDetection",
    "OrchestratorState",
    "OrchestratorStateMachine",
    "PreflightWarning",
    "PreflightWarningKind",
    "StateTransition",
    "ToolCallLoopDetector",
    "agent_failed_event",
    "agent_invoked_event",
    "agent_succeeded_event",
    "budget_exceeded_event",
    "compose_preflight_message",
    "cycle_aborted_event",
    "cycle_completed_event",
    "cycle_started_event",
    "load_jsonl",
    "loop_detected_event",
    "new_cycle_id",
    "new_event_id",
    "scan_text_repetition",
    "scan_tool_repetition",
    "state_transition_event",
]
