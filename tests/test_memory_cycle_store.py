"""Tests for the episodic CycleStore."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from caqrs.memory import CycleIndexEntry, CycleStore
from caqrs.orchestrator import (
    CycleArtifacts,
    CycleEvent,
    CycleEventKind,
    CycleResult,
    EventLog,
    OrchestratorState,
    cycle_aborted_event,
    cycle_completed_event,
    cycle_started_event,
    new_cycle_id,
    new_event_id,
)
from caqrs.schemas.common import RunMetadata, new_run_id
from caqrs.schemas.observer import (
    AssetSnapshot,
    ObserverArtifact,
)


def _meta() -> RunMetadata:
    return RunMetadata(
        run_id=new_run_id(),
        parent_id=None,
        agent_name="observer",
        model_id="test",
        created_at=datetime.now(UTC),
        llm_cost_usd=Decimal(0),
        latency_ms=0,
        token_in=0,
        token_out=0,
    )


def _observer_artifact() -> ObserverArtifact:
    return ObserverArtifact(
        metadata=_meta(),
        universe=("AAPL",),
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        regime_summary="up",
        asset_snapshots=(AssetSnapshot(ticker="AAPL", last_close=Decimal("180")),),
        news_themes=(),
        macro_notes="",
        data_quality_notes=(),
    )


def _completed_result(*, cycle_id: str) -> CycleResult:
    return CycleResult(
        cycle_id=cycle_id,
        terminal_state=OrchestratorState.AUDITING,
        artifacts=CycleArtifacts(observer=_observer_artifact()),
        aborted_reason=None,
        total_token_in=42,
        total_token_out=17,
    )


def _build_completed_log(*, cycle_id: str) -> EventLog:
    log = EventLog()
    log.append(cycle_started_event(cycle_id=cycle_id))
    log.append(
        cycle_completed_event(
            cycle_id=cycle_id,
            terminal_state="auditing",
            artifacts_emitted=1,
            total_token_in=42,
            total_token_out=17,
        ),
    )
    return log


# === Save / load round trip ===


def test_save_creates_per_cycle_directory(tmp_path: Path) -> None:
    cycle_id = new_cycle_id()
    store = CycleStore(root=tmp_path)
    cycle_dir = store.save(
        result=_completed_result(cycle_id=cycle_id),
        events=_build_completed_log(cycle_id=cycle_id).events,
    )
    assert cycle_dir == tmp_path / "cycles" / cycle_id
    assert (cycle_dir / "result.json").exists()
    assert (cycle_dir / "events.jsonl").exists()


def test_save_then_load_round_trips_result(tmp_path: Path) -> None:
    cycle_id = new_cycle_id()
    store = CycleStore(root=tmp_path)
    original = _completed_result(cycle_id=cycle_id)
    store.save(result=original, events=_build_completed_log(cycle_id=cycle_id).events)

    loaded_result, loaded_events = store.load(cycle_id)
    assert loaded_result == original
    assert len(loaded_events) == 2


def test_load_raises_when_cycle_missing(tmp_path: Path) -> None:
    store = CycleStore(root=tmp_path)
    with pytest.raises(FileNotFoundError):
        store.load("does-not-exist")


# === Index ===


def test_save_appends_to_rolling_index(tmp_path: Path) -> None:
    store = CycleStore(root=tmp_path)
    a = new_cycle_id()
    b = new_cycle_id()
    store.save(result=_completed_result(cycle_id=a), events=_build_completed_log(cycle_id=a).events)
    store.save(result=_completed_result(cycle_id=b), events=_build_completed_log(cycle_id=b).events)

    entries = store.index_entries()
    assert len(entries) == 2
    assert {e.cycle_id for e in entries} == {a, b}
    assert all(isinstance(e, CycleIndexEntry) for e in entries)


def test_index_entry_carries_summary_fields(tmp_path: Path) -> None:
    cycle_id = new_cycle_id()
    store = CycleStore(root=tmp_path)
    store.save(
        result=_completed_result(cycle_id=cycle_id),
        events=_build_completed_log(cycle_id=cycle_id).events,
    )
    entry = store.index_entries()[0]
    assert entry.cycle_id == cycle_id
    assert entry.terminal_state is OrchestratorState.AUDITING
    assert entry.aborted_reason is None
    assert entry.artifacts_count == 1
    assert entry.total_token_in == 42
    assert entry.total_token_out == 17
    assert entry.started_at <= entry.ended_at


def test_index_entry_for_aborted_cycle(tmp_path: Path) -> None:
    cycle_id = new_cycle_id()
    log = EventLog()
    log.append(cycle_started_event(cycle_id=cycle_id))
    log.append(
        cycle_aborted_event(
            cycle_id=cycle_id,
            reason="observer: timeout",
            at_state="observing",
        ),
    )
    aborted = CycleResult(
        cycle_id=cycle_id,
        terminal_state=OrchestratorState.ERROR,
        artifacts=CycleArtifacts(),
        aborted_reason="observer: timeout",
        total_token_in=0,
        total_token_out=0,
    )
    store = CycleStore(root=tmp_path)
    store.save(result=aborted, events=log.events)

    entry = store.index_entries()[0]
    assert entry.terminal_state is OrchestratorState.ERROR
    assert entry.aborted_reason == "observer: timeout"
    assert entry.artifacts_count == 0


def test_index_entry_is_frozen(tmp_path: Path) -> None:
    cycle_id = new_cycle_id()
    store = CycleStore(root=tmp_path)
    store.save(
        result=_completed_result(cycle_id=cycle_id),
        events=_build_completed_log(cycle_id=cycle_id).events,
    )
    entry = store.index_entries()[0]
    with pytest.raises(ValidationError, match="frozen"):
        entry.cycle_id = "abc"  # type: ignore[misc]


# === Listing ===


def test_list_cycle_ids_empty_when_root_missing(tmp_path: Path) -> None:
    store = CycleStore(root=tmp_path / "fresh")
    assert store.list_cycle_ids() == ()


def test_list_cycle_ids_returns_sorted(tmp_path: Path) -> None:
    store = CycleStore(root=tmp_path)
    ids = sorted([new_cycle_id(), new_cycle_id(), new_cycle_id()])
    for cid in ids:
        store.save(
            result=_completed_result(cycle_id=cid),
            events=_build_completed_log(cycle_id=cid).events,
        )
    assert store.list_cycle_ids() == tuple(ids)


def test_index_entries_empty_when_no_saves(tmp_path: Path) -> None:
    store = CycleStore(root=tmp_path)
    assert store.index_entries() == ()


# === Atomicity / idempotency ===


def test_save_overwrites_existing_cycle_files(tmp_path: Path) -> None:
    """Re-saving the same cycle id replaces result/events but appends a new index row."""
    cycle_id = new_cycle_id()
    store = CycleStore(root=tmp_path)
    store.save(
        result=_completed_result(cycle_id=cycle_id),
        events=_build_completed_log(cycle_id=cycle_id).events,
    )
    # Re-save with a different result
    updated = CycleResult(
        cycle_id=cycle_id,
        terminal_state=OrchestratorState.AUDITING,
        artifacts=CycleArtifacts(observer=_observer_artifact()),
        aborted_reason=None,
        total_token_in=999,
        total_token_out=999,
    )
    store.save(result=updated, events=_build_completed_log(cycle_id=cycle_id).events)
    loaded, _ = store.load(cycle_id)
    assert loaded.total_token_in == 999

    entries = store.index_entries()
    assert len(entries) == 2  # both saves recorded; index is append-only


# === Episodic prune ===


def _events_with_explicit_times(
    *,
    cycle_id: str,
    started_at: datetime,
    ended_at: datetime,
) -> tuple[CycleEvent, ...]:
    """Build a CYCLE_STARTED + CYCLE_COMPLETED pair with chosen timestamps.

    Bypasses the typed builders so the prune tests can construct cycles
    that look as if they ran days/weeks ago without freezing the clock.
    """
    started = CycleEvent(
        event_id=new_event_id(),
        cycle_id=cycle_id,
        kind=CycleEventKind.CYCLE_STARTED,
        timestamp=started_at,
        payload={},
    )
    ended = CycleEvent(
        event_id=new_event_id(),
        cycle_id=cycle_id,
        kind=CycleEventKind.CYCLE_COMPLETED,
        timestamp=ended_at,
        payload={
            "terminal_state": "auditing",
            "artifacts_emitted": 1,
            "total_token_in": 0,
            "total_token_out": 0,
        },
    )
    return (started, ended)


def _save_dated_cycle(
    *,
    store: CycleStore,
    cycle_id: str,
    ended_at: datetime,
) -> None:
    started_at = ended_at - timedelta(seconds=1)
    events = _events_with_explicit_times(
        cycle_id=cycle_id,
        started_at=started_at,
        ended_at=ended_at,
    )
    result = CycleResult(
        cycle_id=cycle_id,
        terminal_state=OrchestratorState.AUDITING,
        artifacts=CycleArtifacts(observer=_observer_artifact()),
        aborted_reason=None,
        total_token_in=0,
        total_token_out=0,
    )
    store.save(result=result, events=events)


def test_prune_drops_cycles_older_than_cutoff(tmp_path: Path) -> None:
    store = CycleStore(root=tmp_path)
    now = datetime(2026, 4, 27, tzinfo=UTC)
    old_id = new_cycle_id()
    fresh_id = new_cycle_id()
    _save_dated_cycle(store=store, cycle_id=old_id, ended_at=now - timedelta(days=45))
    _save_dated_cycle(store=store, cycle_id=fresh_id, ended_at=now - timedelta(days=5))

    pruned = store.prune_older_than(now - timedelta(days=30))

    assert pruned == (old_id,)
    assert store.list_cycle_ids() == (fresh_id,)


def test_prune_returns_empty_when_no_cycles_match(tmp_path: Path) -> None:
    store = CycleStore(root=tmp_path)
    now = datetime(2026, 4, 27, tzinfo=UTC)
    fresh_id = new_cycle_id()
    _save_dated_cycle(store=store, cycle_id=fresh_id, ended_at=now - timedelta(days=5))

    pruned = store.prune_older_than(now - timedelta(days=30))

    assert pruned == ()
    assert store.list_cycle_ids() == (fresh_id,)


def test_prune_removes_per_cycle_files(tmp_path: Path) -> None:
    store = CycleStore(root=tmp_path)
    now = datetime(2026, 4, 27, tzinfo=UTC)
    old_id = new_cycle_id()
    _save_dated_cycle(store=store, cycle_id=old_id, ended_at=now - timedelta(days=60))

    cycle_dir = tmp_path / "cycles" / old_id
    assert cycle_dir.exists()  # baseline

    store.prune_older_than(now - timedelta(days=30))

    assert not cycle_dir.exists()


def test_prune_rewrites_index_to_drop_pruned_entries(tmp_path: Path) -> None:
    store = CycleStore(root=tmp_path)
    now = datetime(2026, 4, 27, tzinfo=UTC)
    old_id = new_cycle_id()
    fresh_id = new_cycle_id()
    _save_dated_cycle(store=store, cycle_id=old_id, ended_at=now - timedelta(days=45))
    _save_dated_cycle(store=store, cycle_id=fresh_id, ended_at=now - timedelta(days=5))

    store.prune_older_than(now - timedelta(days=30))

    surviving = store.index_entries()
    assert len(surviving) == 1
    assert surviving[0].cycle_id == fresh_id


def test_prune_is_idempotent(tmp_path: Path) -> None:
    store = CycleStore(root=tmp_path)
    now = datetime(2026, 4, 27, tzinfo=UTC)
    old_id = new_cycle_id()
    _save_dated_cycle(store=store, cycle_id=old_id, ended_at=now - timedelta(days=60))

    first = store.prune_older_than(now - timedelta(days=30))
    second = store.prune_older_than(now - timedelta(days=30))

    assert first == (old_id,)
    assert second == ()


def test_prune_on_empty_store_returns_empty(tmp_path: Path) -> None:
    """Calling prune before anything was saved must not raise."""
    store = CycleStore(root=tmp_path)
    pruned = store.prune_older_than(datetime(2026, 1, 1, tzinfo=UTC))
    assert pruned == ()


def test_prune_uses_ended_at_not_started_at(tmp_path: Path) -> None:
    """A long-running cycle that started old but ended recently must
    not be pruned; the cutoff applies to ended_at."""
    store = CycleStore(root=tmp_path)
    now = datetime(2026, 4, 27, tzinfo=UTC)
    cycle_id = new_cycle_id()
    started_at = now - timedelta(days=40)
    ended_at = now - timedelta(days=5)
    events = _events_with_explicit_times(
        cycle_id=cycle_id,
        started_at=started_at,
        ended_at=ended_at,
    )
    result = CycleResult(
        cycle_id=cycle_id,
        terminal_state=OrchestratorState.AUDITING,
        artifacts=CycleArtifacts(observer=_observer_artifact()),
        aborted_reason=None,
    )
    store.save(result=result, events=events)

    pruned = store.prune_older_than(now - timedelta(days=30))

    assert pruned == ()
    assert store.list_cycle_ids() == (cycle_id,)


def test_prune_handles_resaved_cycle_with_multiple_index_rows(tmp_path: Path) -> None:
    """A cycle re-saved twice has two index rows (append-only). Prune
    should remove both rows and the on-disk dir if the latest row is
    older than the cutoff."""
    store = CycleStore(root=tmp_path)
    now = datetime(2026, 4, 27, tzinfo=UTC)
    cycle_id = new_cycle_id()
    _save_dated_cycle(store=store, cycle_id=cycle_id, ended_at=now - timedelta(days=60))
    _save_dated_cycle(store=store, cycle_id=cycle_id, ended_at=now - timedelta(days=50))

    pruned = store.prune_older_than(now - timedelta(days=30))

    assert pruned == (cycle_id,)
    assert store.list_cycle_ids() == ()
    assert store.index_entries() == ()
