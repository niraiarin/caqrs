"""Tests for OrchestratorStateMachine."""

import pytest

from caqrs.orchestrator import (
    OrchestratorState,
    OrchestratorStateMachine,
    StateTransition,
)


def test_initial_state_is_idle() -> None:
    sm = OrchestratorStateMachine()
    assert sm.state is OrchestratorState.IDLE
    assert sm.is_in(OrchestratorState.IDLE)


def test_initial_state_can_be_overridden() -> None:
    sm = OrchestratorStateMachine(initial=OrchestratorState.OBSERVING)
    assert sm.state is OrchestratorState.OBSERVING


def test_valid_transition_returns_true_and_advances_state() -> None:
    sm = OrchestratorStateMachine()
    assert sm.transition(OrchestratorState.OBSERVING) is True
    assert sm.state is OrchestratorState.OBSERVING


def test_invalid_transition_returns_false_and_keeps_state() -> None:
    sm = OrchestratorStateMachine()
    # IDLE → AUDITING is not a valid jump
    assert sm.transition(OrchestratorState.AUDITING) is False
    assert sm.state is OrchestratorState.IDLE


def test_can_transition_to_reflects_validity() -> None:
    sm = OrchestratorStateMachine()
    assert sm.can_transition_to(OrchestratorState.OBSERVING) is True
    assert sm.can_transition_to(OrchestratorState.AUDITING) is False


def test_full_happy_path_pipeline() -> None:
    sm = OrchestratorStateMachine()
    pipeline = [
        OrchestratorState.OBSERVING,
        OrchestratorState.HYPOTHESIZING,
        OrchestratorState.SCRUTINIZING,
        OrchestratorState.RESEARCHING,
        OrchestratorState.AUDITING,
        OrchestratorState.DECIDING,
        OrchestratorState.REPORTING,
        OrchestratorState.IDLE,
    ]
    for target in pipeline:
        assert sm.transition(target) is True
    assert sm.is_in(OrchestratorState.IDLE)


def test_skeptic_kills_hypothesis_returns_to_idle() -> None:
    sm = OrchestratorStateMachine()
    sm.transition(OrchestratorState.OBSERVING)
    sm.transition(OrchestratorState.HYPOTHESIZING)
    sm.transition(OrchestratorState.SCRUTINIZING)
    assert sm.transition(OrchestratorState.IDLE) is True
    assert sm.is_in(OrchestratorState.IDLE)


def test_any_state_can_transition_to_error() -> None:
    for source in OrchestratorState:
        sm = OrchestratorStateMachine(initial=source)
        assert sm.transition(OrchestratorState.ERROR) is True
        assert sm.state is OrchestratorState.ERROR


def test_error_transitions_back_to_idle() -> None:
    sm = OrchestratorStateMachine(initial=OrchestratorState.ERROR)
    assert sm.transition(OrchestratorState.IDLE) is True
    assert sm.is_in(OrchestratorState.IDLE)


def test_error_cannot_jump_to_arbitrary_state() -> None:
    sm = OrchestratorStateMachine(initial=OrchestratorState.ERROR)
    assert sm.transition(OrchestratorState.OBSERVING) is False
    assert sm.is_in(OrchestratorState.ERROR)


def test_listener_fires_on_successful_transition() -> None:
    sm = OrchestratorStateMachine()
    fired: list[StateTransition] = []
    sm.on_transition(fired.append)

    assert sm.transition(OrchestratorState.OBSERVING) is True
    assert len(fired) == 1
    assert fired[0].src is OrchestratorState.IDLE
    assert fired[0].dst is OrchestratorState.OBSERVING


def test_listener_does_not_fire_on_failed_transition() -> None:
    sm = OrchestratorStateMachine()
    fired: list[StateTransition] = []
    sm.on_transition(fired.append)

    assert sm.transition(OrchestratorState.AUDITING) is False
    assert fired == []


def test_multiple_listeners_each_receive_event() -> None:
    sm = OrchestratorStateMachine()
    a: list[StateTransition] = []
    b: list[StateTransition] = []
    sm.on_transition(a.append)
    sm.on_transition(b.append)

    sm.transition(OrchestratorState.OBSERVING)
    assert len(a) == 1
    assert len(b) == 1


def test_state_transition_record_is_frozen() -> None:
    record = StateTransition(src=OrchestratorState.IDLE, dst=OrchestratorState.OBSERVING)
    with pytest.raises(ValueError, match="frozen"):
        record.src = OrchestratorState.OBSERVING  # type: ignore[misc]
