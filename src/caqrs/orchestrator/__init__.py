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
"""

from caqrs.orchestrator.budget import (
    BudgetGuard,
    BudgetStatus,
    BudgetStatusKind,
    CycleBudget,
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
    "BudgetGuard",
    "BudgetStatus",
    "BudgetStatusKind",
    "CallRecord",
    "CycleBudget",
    "CycleEvent",
    "CycleEventKind",
    "EventLog",
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
