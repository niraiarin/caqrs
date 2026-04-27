"""Orchestrator subsystem: state machine, loop detection, queue, budget.

P1.2.a ships the foundational pieces: ``OrchestratorState`` enum,
``OrchestratorStateMachine`` with whitelist transitions, and
``ToolCallLoopDetector`` ported from Mercury's ``src/core/agent.ts``
(see ``docs/research/mercury-survey/03-agent-harness.md`` and
``docs/research/mercury-survey/10-lifecycle.md``).

Subsequent slices add: preflight warning injection, queue with
reentrancy guard, per-cycle budget, heartbeat, scheduler, event log.
"""

from caqrs.orchestrator.loop_detector import (
    CallRecord,
    LoopDetection,
    ToolCallLoopDetector,
)
from caqrs.orchestrator.state import OrchestratorState
from caqrs.orchestrator.state_machine import OrchestratorStateMachine, StateTransition

__all__ = [
    "CallRecord",
    "LoopDetection",
    "OrchestratorState",
    "OrchestratorStateMachine",
    "StateTransition",
    "ToolCallLoopDetector",
]
