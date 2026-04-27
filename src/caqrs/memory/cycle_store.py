"""Episodic cycle archive.

A :class:`CycleStore` persists each completed (or aborted) cycle as
a directory under ``root/cycles/<cycle_id>/`` containing:

- ``result.json`` — the :class:`CycleResult` as pretty JSON
- ``events.jsonl`` — every :class:`CycleEvent` for this cycle, one
  JSON object per line

A rolling ``index.jsonl`` at the root summarises every saved cycle
for quick listing without walking the directory tree. The summary is
derived from the result + events at save time so future schema
changes can re-derive it from the authoritative records.

Design notes:

- Saves are atomic per-file (write to ``.tmp`` then rename) so a
  crash mid-save leaves the previous state intact for that cycle.
- Loads are pure: a :class:`CycleStore` does not cache anything.
"""

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from caqrs.orchestrator.cycle_runner import CycleResult
from caqrs.orchestrator.event_log import load_jsonl
from caqrs.orchestrator.events import CycleEvent, CycleEventKind
from caqrs.orchestrator.state import OrchestratorState


class CycleIndexEntry(BaseModel):
    """One row in the rolling index."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    cycle_id: str
    terminal_state: OrchestratorState
    aborted_reason: str | None
    artifacts_count: int
    total_token_in: int
    total_token_out: int
    started_at: datetime
    ended_at: datetime


class CycleStore:
    """Persists cycles to disk under ``root/``."""

    def __init__(self, *, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    @property
    def _cycles_dir(self) -> Path:
        return self._root / "cycles"

    @property
    def _index_path(self) -> Path:
        return self._root / "index.jsonl"

    def save(
        self,
        *,
        result: CycleResult,
        events: Iterable[CycleEvent],
    ) -> Path:
        """Persist a cycle. Returns the per-cycle directory."""
        cycle_dir = self._cycles_dir / result.cycle_id
        cycle_dir.mkdir(parents=True, exist_ok=True)

        events_tuple = tuple(events)

        _atomic_write_text(
            cycle_dir / "result.json",
            result.model_dump_json(indent=2),
        )
        _atomic_write_text(
            cycle_dir / "events.jsonl",
            "\n".join(e.model_dump_json() for e in events_tuple) + ("\n" if events_tuple else ""),
        )

        entry = _build_index_entry(result=result, events=events_tuple)
        with self._index_path.open("a", encoding="utf-8") as f:
            f.write(entry.model_dump_json() + "\n")

        return cycle_dir

    def load(self, cycle_id: str) -> tuple[CycleResult, tuple[CycleEvent, ...]]:
        """Load a previously saved cycle."""
        cycle_dir = self._cycles_dir / cycle_id
        result_path = cycle_dir / "result.json"
        events_path = cycle_dir / "events.jsonl"

        if not result_path.exists():
            msg = f"cycle {cycle_id!r} not found at {result_path}"
            raise FileNotFoundError(msg)

        result = CycleResult.model_validate_json(result_path.read_text(encoding="utf-8"))
        events = load_jsonl(events_path)
        return result, events

    def list_cycle_ids(self) -> tuple[str, ...]:
        if not self._cycles_dir.exists():
            return ()
        return tuple(sorted(p.name for p in self._cycles_dir.iterdir() if p.is_dir()))

    def index_entries(self) -> tuple[CycleIndexEntry, ...]:
        if not self._index_path.exists():
            return ()
        entries: list[CycleIndexEntry] = []
        for line in self._index_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            entries.append(CycleIndexEntry.model_validate_json(stripped))
        return tuple(entries)

    def prune_older_than(self, cutoff: datetime) -> tuple[str, ...]:
        """Drop cycles whose latest ``ended_at`` precedes ``cutoff``.

        Returns the cycle ids removed in the order they were saved. The
        per-cycle directories are deleted and ``index.jsonl`` is
        rewritten atomically to drop *every* index row referencing those
        ids (a re-saved cycle has multiple rows; all go).
        """
        entries = self.index_entries()
        if not entries:
            return ()

        latest_end_per_cycle: dict[str, datetime] = {}
        for entry in entries:
            current = latest_end_per_cycle.get(entry.cycle_id)
            if current is None or entry.ended_at > current:
                latest_end_per_cycle[entry.cycle_id] = entry.ended_at

        pruned: list[str] = []
        for entry in entries:
            if entry.cycle_id in pruned:
                continue
            if latest_end_per_cycle[entry.cycle_id] < cutoff:
                pruned.append(entry.cycle_id)

        if not pruned:
            return ()

        for cycle_id in pruned:
            self._remove_cycle_dir(cycle_id)

        survivors = tuple(e for e in entries if e.cycle_id not in set(pruned))
        self._rewrite_index(survivors)
        return tuple(pruned)

    def _remove_cycle_dir(self, cycle_id: str) -> None:
        cycle_dir = self._cycles_dir / cycle_id
        if not cycle_dir.exists():
            return
        # CycleStore writes only result.json + events.jsonl; deleting
        # those files plus the directory keeps the operation defensive
        # (no rmtree on directories we did not author).
        for child in cycle_dir.iterdir():
            child.unlink()
        cycle_dir.rmdir()

    def _rewrite_index(self, survivors: tuple[CycleIndexEntry, ...]) -> None:
        if not survivors:
            if self._index_path.exists():
                self._index_path.unlink()
            return
        content = "\n".join(entry.model_dump_json() for entry in survivors) + "\n"
        _atomic_write_text(self._index_path, content)


def _atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` via a tmp+rename so a crash leaves
    the previous file intact.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _build_index_entry(
    *,
    result: CycleResult,
    events: tuple[CycleEvent, ...],
) -> CycleIndexEntry:
    started_at = _first_event_ts(events, kind=CycleEventKind.CYCLE_STARTED)
    ended_at = _last_event_ts(
        events,
        kinds=(CycleEventKind.CYCLE_COMPLETED, CycleEventKind.CYCLE_ABORTED),
    )
    return CycleIndexEntry(
        cycle_id=result.cycle_id,
        terminal_state=result.terminal_state,
        aborted_reason=result.aborted_reason,
        artifacts_count=_count_artifacts(result),
        total_token_in=result.total_token_in,
        total_token_out=result.total_token_out,
        started_at=started_at,
        ended_at=ended_at,
    )


def _first_event_ts(events: tuple[CycleEvent, ...], *, kind: CycleEventKind) -> datetime:
    for e in events:
        if e.kind is kind:
            return e.timestamp
    msg = f"no {kind} event in cycle event log"
    raise ValueError(msg)


def _last_event_ts(
    events: tuple[CycleEvent, ...],
    *,
    kinds: tuple[CycleEventKind, ...],
) -> datetime:
    for e in reversed(events):
        if e.kind in kinds:
            return e.timestamp
    msg = f"no terminal event ({kinds}) in cycle event log"
    raise ValueError(msg)


def _count_artifacts(result: CycleResult) -> int:
    a = result.artifacts
    return sum(
        1
        for v in (a.observer, a.hypothesis, a.skeptic, a.research, a.backtest, a.audit)
        if v is not None
    )
