"""Whitelist state machine for the CAQRS orchestrator.

Ported from Mercury ``src/core/lifecycle.ts`` (44 lines). The
adaptation: research-cycle phases instead of Mercury's
unborn/birthing/onboarding/idle/thinking/responding states. See
``docs/research/mercury-survey/10-lifecycle.md`` for the comparison.

The valid-transition set is a frozenset of (from, to) tuples. Listener
callbacks fire on every successful transition for downstream
observability (event log, structured logging).
"""

from collections.abc import Callable
from typing import Final

from pydantic import BaseModel, ConfigDict

from caqrs.orchestrator.state import OrchestratorState

_S = OrchestratorState


class StateTransition(BaseModel):
    """A transition record fired to listeners after a successful change."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    src: OrchestratorState
    dst: OrchestratorState


_VALID_TRANSITIONS: Final[frozenset[tuple[OrchestratorState, OrchestratorState]]] = frozenset(
    {
        (_S.IDLE, _S.OBSERVING),
        (_S.OBSERVING, _S.HYPOTHESIZING),
        (_S.HYPOTHESIZING, _S.SCRUTINIZING),
        (_S.HYPOTHESIZING, _S.IDLE),  # low-quality hypothesis rejected pre-skeptic
        (_S.SCRUTINIZING, _S.RESEARCHING),
        (_S.SCRUTINIZING, _S.IDLE),  # skeptic kills the hypothesis
        (_S.RESEARCHING, _S.AUDITING),
        (_S.RESEARCHING, _S.IDLE),  # research fails (insufficient data, etc.)
        (_S.AUDITING, _S.DECIDING),
        (_S.AUDITING, _S.IDLE),  # acceptance criteria fail
        (_S.DECIDING, _S.REPORTING),
        (_S.DECIDING, _S.IDLE),  # decision is "do not act"
        (_S.REPORTING, _S.IDLE),
        # Error path: any state may transition to ERROR
        *((s, _S.ERROR) for s in _S),
        # Recovery: ERROR back to IDLE (operator-acknowledged or auto)
        (_S.ERROR, _S.IDLE),
    },
)


_StateListener = Callable[[StateTransition], None]


class OrchestratorStateMachine:
    """Whitelist state machine with listener fan-out.

    Initial state is ``IDLE``. ``transition(target)`` validates the
    move against ``_VALID_TRANSITIONS`` and returns whether the move
    was accepted. Rejected moves leave the state unchanged.
    """

    def __init__(self, *, initial: OrchestratorState = OrchestratorState.IDLE) -> None:
        self._state = initial
        self._listeners: list[_StateListener] = []

    @property
    def state(self) -> OrchestratorState:
        return self._state

    def is_in(self, candidate: OrchestratorState) -> bool:
        return self._state == candidate

    def can_transition_to(self, target: OrchestratorState) -> bool:
        return (self._state, target) in _VALID_TRANSITIONS

    def transition(self, target: OrchestratorState) -> bool:
        """Attempt a transition. Returns True on success.

        Successful transitions fire all registered listeners with a
        ``StateTransition`` record. Failed transitions leave the state
        unchanged and return False; callers are expected to log or
        recover.
        """
        if (self._state, target) not in _VALID_TRANSITIONS:
            return False
        record = StateTransition(src=self._state, dst=target)
        self._state = target
        for listener in self._listeners:
            listener(record)
        return True

    def on_transition(self, listener: _StateListener) -> None:
        """Register a callback fired on every successful transition."""
        self._listeners.append(listener)
