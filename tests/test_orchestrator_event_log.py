"""Tests for cycle event types, builders, and the append-only EventLog."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from caqrs.orchestrator import (
    CycleEvent,
    CycleEventKind,
    EventLog,
    agent_failed_event,
    agent_invoked_event,
    agent_succeeded_event,
    budget_exceeded_event,
    cycle_aborted_event,
    cycle_completed_event,
    cycle_started_event,
    load_jsonl,
    loop_detected_event,
    new_cycle_id,
    new_event_id,
    state_transition_event,
)

# === ID generators ===


def test_new_cycle_id_is_sixteen_hex_chars() -> None:
    cycle_id = new_cycle_id()
    assert len(cycle_id) == 16
    assert all(c in "0123456789abcdef" for c in cycle_id)


def test_new_cycle_id_is_unique() -> None:
    a, b = new_cycle_id(), new_cycle_id()
    assert a != b


def test_new_event_id_is_sixteen_hex_chars() -> None:
    event_id = new_event_id()
    assert len(event_id) == 16
    assert all(c in "0123456789abcdef" for c in event_id)


# === Typed builders ===


def test_cycle_started_event_minimal() -> None:
    cycle_id = new_cycle_id()
    event = cycle_started_event(cycle_id=cycle_id)
    assert event.cycle_id == cycle_id
    assert event.kind is CycleEventKind.CYCLE_STARTED
    assert event.payload == {}


def test_cycle_started_event_with_observer_input_run_id() -> None:
    cycle_id = new_cycle_id()
    obs_id = "abc123"
    event = cycle_started_event(cycle_id=cycle_id, observer_input_run_id=obs_id)
    assert event.payload["observer_input_run_id"] == obs_id


def test_cycle_completed_event_carries_aggregate_stats() -> None:
    cycle_id = new_cycle_id()
    event = cycle_completed_event(
        cycle_id=cycle_id,
        terminal_state="reporting",
        artifacts_emitted=5,
        total_token_in=1234,
        total_token_out=567,
    )
    assert event.kind is CycleEventKind.CYCLE_COMPLETED
    assert event.payload["terminal_state"] == "reporting"
    assert event.payload["artifacts_emitted"] == 5
    assert event.payload["total_token_in"] == 1234


def test_cycle_aborted_event() -> None:
    event = cycle_aborted_event(
        cycle_id=new_cycle_id(),
        reason="loop detected",
        at_state="hypothesizing",
    )
    assert event.kind is CycleEventKind.CYCLE_ABORTED
    assert event.payload == {"reason": "loop detected", "at_state": "hypothesizing"}


def test_agent_invoked_event() -> None:
    event = agent_invoked_event(
        cycle_id=new_cycle_id(),
        agent_name="hypothesis",
        run_id="r1",
    )
    assert event.kind is CycleEventKind.AGENT_INVOKED
    assert event.payload == {"agent_name": "hypothesis", "run_id": "r1"}


def test_agent_succeeded_event_carries_metrics() -> None:
    event = agent_succeeded_event(
        cycle_id=new_cycle_id(),
        agent_name="hypothesis",
        run_id="r1",
        output_schema="HypothesisCard",
        token_in=42,
        token_out=17,
        latency_ms=99,
    )
    assert event.payload["token_in"] == 42
    assert event.payload["latency_ms"] == 99


def test_agent_failed_event() -> None:
    event = agent_failed_event(
        cycle_id=new_cycle_id(),
        agent_name="skeptic",
        run_id="r2",
        error="AuthError: token expired",
    )
    assert event.kind is CycleEventKind.AGENT_FAILED
    assert "AuthError" in event.payload["error"]


def test_state_transition_event() -> None:
    event = state_transition_event(
        cycle_id=new_cycle_id(),
        src="idle",
        dst="observing",
    )
    assert event.kind is CycleEventKind.STATE_TRANSITION
    assert event.payload == {"src": "idle", "dst": "observing"}


def test_loop_detected_event() -> None:
    event = loop_detected_event(
        cycle_id=new_cycle_id(),
        rule="identical",
        tool="approve_scope",
        count=3,
        message="hard abort",
    )
    assert event.kind is CycleEventKind.LOOP_DETECTED
    assert event.payload["rule"] == "identical"
    assert event.payload["count"] == 3


def test_budget_exceeded_event() -> None:
    event = budget_exceeded_event(
        cycle_id=new_cycle_id(),
        budget_kind="token",
        consumed=1100,
        cap=1000,
    )
    assert event.kind is CycleEventKind.BUDGET_EXCEEDED
    assert event.payload == {"budget_kind": "token", "consumed": 1100, "cap": 1000}


# === Frozen / extra=forbid ===


def test_event_is_frozen() -> None:
    event = cycle_started_event(cycle_id=new_cycle_id())
    with pytest.raises(ValueError, match="frozen"):
        event.kind = CycleEventKind.CYCLE_COMPLETED  # type: ignore[misc]


def test_event_rejects_extras() -> None:
    with pytest.raises(ValidationError):
        CycleEvent(  # type: ignore[call-arg]
            event_id=new_event_id(),
            cycle_id=new_cycle_id(),
            kind=CycleEventKind.CYCLE_STARTED,
            timestamp=cycle_started_event(cycle_id=new_cycle_id()).timestamp,
            payload={},
            extra="nope",
        )


# === EventLog basic operations ===


def test_event_log_starts_empty() -> None:
    log = EventLog()
    assert len(log) == 0
    assert log.events == ()


def test_event_log_append_records_in_order() -> None:
    log = EventLog()
    cycle_id = new_cycle_id()
    log.append(cycle_started_event(cycle_id=cycle_id))
    log.append(agent_invoked_event(cycle_id=cycle_id, agent_name="observer", run_id="r1"))
    assert len(log) == 2
    kinds = [e.kind for e in log.events]
    assert kinds == [CycleEventKind.CYCLE_STARTED, CycleEventKind.AGENT_INVOKED]


def test_event_log_extend_appends_in_order() -> None:
    log = EventLog()
    cycle_id = new_cycle_id()
    log.extend(
        [
            cycle_started_event(cycle_id=cycle_id),
            agent_invoked_event(cycle_id=cycle_id, agent_name="x", run_id="r"),
        ],
    )
    assert len(log) == 2


def test_event_log_filter_by_kind() -> None:
    log = EventLog()
    cycle_id = new_cycle_id()
    log.append(cycle_started_event(cycle_id=cycle_id))
    log.append(agent_invoked_event(cycle_id=cycle_id, agent_name="x", run_id="r1"))
    log.append(agent_invoked_event(cycle_id=cycle_id, agent_name="y", run_id="r2"))
    invoked = log.filter_by_kind(CycleEventKind.AGENT_INVOKED)
    assert len(invoked) == 2
    assert all(e.kind is CycleEventKind.AGENT_INVOKED for e in invoked)


def test_event_log_filter_by_cycle() -> None:
    log = EventLog()
    cycle_a = new_cycle_id()
    cycle_b = new_cycle_id()
    log.append(cycle_started_event(cycle_id=cycle_a))
    log.append(cycle_started_event(cycle_id=cycle_b))
    log.append(agent_invoked_event(cycle_id=cycle_a, agent_name="x", run_id="r"))
    a_events = log.filter_by_cycle(cycle_a)
    assert len(a_events) == 2
    assert all(e.cycle_id == cycle_a for e in a_events)


# === Listeners ===


def test_listener_fires_on_append() -> None:
    log = EventLog()
    received: list[CycleEvent] = []
    log.on_event(received.append)
    event = cycle_started_event(cycle_id=new_cycle_id())
    log.append(event)
    assert received == [event]


def test_listener_fires_for_each_event_in_extend() -> None:
    log = EventLog()
    received: list[CycleEvent] = []
    log.on_event(received.append)
    cycle_id = new_cycle_id()
    log.extend(
        [
            cycle_started_event(cycle_id=cycle_id),
            cycle_completed_event(
                cycle_id=cycle_id,
                terminal_state="reporting",
                artifacts_emitted=0,
            ),
        ],
    )
    assert len(received) == 2


def test_multiple_listeners_each_receive_event() -> None:
    log = EventLog()
    a: list[CycleEvent] = []
    b: list[CycleEvent] = []
    log.on_event(a.append)
    log.on_event(b.append)
    log.append(cycle_started_event(cycle_id=new_cycle_id()))
    assert len(a) == len(b) == 1


# === JSONL persistence ===


def test_event_log_persists_to_jsonl(tmp_path: Path) -> None:
    target = tmp_path / "cycles" / "abc.jsonl"
    log = EventLog(persist_to=target)
    cycle_id = new_cycle_id()
    log.append(cycle_started_event(cycle_id=cycle_id))
    log.append(
        cycle_completed_event(
            cycle_id=cycle_id,
            terminal_state="reporting",
            artifacts_emitted=2,
        ),
    )

    content = target.read_text(encoding="utf-8")
    lines = [line for line in content.splitlines() if line.strip()]
    assert len(lines) == 2


def test_load_jsonl_round_trips(tmp_path: Path) -> None:
    target = tmp_path / "events.jsonl"
    log = EventLog(persist_to=target)
    cycle_id = new_cycle_id()
    log.append(cycle_started_event(cycle_id=cycle_id))
    log.append(
        cycle_completed_event(
            cycle_id=cycle_id,
            terminal_state="reporting",
            artifacts_emitted=0,
        ),
    )

    restored = load_jsonl(target)
    assert len(restored) == 2
    assert restored[0].kind is CycleEventKind.CYCLE_STARTED
    assert restored[1].kind is CycleEventKind.CYCLE_COMPLETED


def test_load_jsonl_returns_empty_when_file_missing(tmp_path: Path) -> None:
    assert load_jsonl(tmp_path / "missing.jsonl") == ()


def test_load_jsonl_skips_blank_lines(tmp_path: Path) -> None:
    target = tmp_path / "with_blanks.jsonl"
    log = EventLog(persist_to=target)
    log.append(cycle_started_event(cycle_id=new_cycle_id()))
    target.write_text(target.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")
    restored = load_jsonl(target)
    assert len(restored) == 1


def test_load_jsonl_raises_on_corruption(tmp_path: Path) -> None:
    target = tmp_path / "corrupt.jsonl"
    target.write_text("not json at all\n", encoding="utf-8")
    with pytest.raises((ValidationError, ValueError)):
        load_jsonl(target)
