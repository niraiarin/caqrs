"""Orchestrator state enum.

The CAQRS orchestrator runs research cycles through a typed pipeline:
Observer → Hypothesis → Skeptic → Research → Auditor → Decision →
Reporting. Each phase corresponds to a state; the state machine in
``state_machine.py`` enforces valid transitions.

Rationale: Mercury's lifecycle (``src/core/lifecycle.ts``) demonstrated
that whitelist transitions caught a class of programming errors
(state changes from inappropriate handlers) at log time; CAQRS adopts
the same pattern with research-domain phases.
"""

from enum import StrEnum


class OrchestratorState(StrEnum):
    """States the orchestrator passes through during a research cycle.

    ``IDLE`` is the resting state between cycles. ``ERROR`` is the
    recovery sink — any state may transition to it; it transitions
    back to ``IDLE`` after the operator acknowledges or the
    orchestrator's own error handler logs the episode.
    """

    IDLE = "idle"
    OBSERVING = "observing"
    HYPOTHESIZING = "hypothesizing"
    SCRUTINIZING = "scrutinizing"
    RESEARCHING = "researching"
    AUDITING = "auditing"
    DECIDING = "deciding"
    REPORTING = "reporting"
    ERROR = "error"
