"""Orchestrator subsystem: state machine, loop detection, preflight, queue, budget.

P1.2.a shipped: ``OrchestratorState`` enum, ``OrchestratorStateMachine`` with
whitelist transitions, ``ToolCallLoopDetector`` ported from Mercury
``src/core/agent.ts`` (see research files 03 and 10).

P1.2.b adds: preflight warning injection (tool-repetition + text-repetition
scanners) ported from Mercury ``src/core/agent.ts:414-458``. Together with
the reactive ``ToolCallLoopDetector`` this forms the dual-loop discipline.

Subsequent slices add: queue with reentrancy guard, per-cycle budget,
heartbeat, scheduler, event log.
"""

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
    "CallRecord",
    "LoopDetection",
    "OrchestratorState",
    "OrchestratorStateMachine",
    "PreflightWarning",
    "PreflightWarningKind",
    "StateTransition",
    "ToolCallLoopDetector",
    "compose_preflight_message",
    "scan_text_repetition",
    "scan_tool_repetition",
]
