"""Append-only event log with optional JSONL persistence.

Mirrors Mercury's episodic log pattern (``src/memory/store.ts`
``EpisodicMemory``) at the cycle granularity: events are appended in
order and never mutated. Persistence is opt-in via a ``Path`` argument;
the in-memory list is always available for the running cycle.

Listener callbacks fire on every append for downstream consumers
(structured logger, future Mercury-skill bridge, regret analysis).
"""

from collections.abc import Callable, Iterable
from pathlib import Path

from caqrs.orchestrator.events import CycleEvent, CycleEventKind

_EventListener = Callable[[CycleEvent], None]


class EventLog:
    """In-memory append-only event log with optional JSONL backing file.

    ``EventLog`` is single-cycle scoped by convention (not enforced):
    instantiate one per cycle, persist to a ``cycles/<cycle_id>.jsonl``
    path, attach listeners that fan-out to structured loggers.
    """

    def __init__(self, *, persist_to: Path | None = None) -> None:
        self._events: list[CycleEvent] = []
        self._persist_to = persist_to
        self._listeners: list[_EventListener] = []
        if persist_to is not None:
            persist_to.parent.mkdir(parents=True, exist_ok=True)

    @property
    def events(self) -> tuple[CycleEvent, ...]:
        """Snapshot of all events recorded so far, oldest-first."""
        return tuple(self._events)

    def append(self, event: CycleEvent) -> None:
        """Record an event. Persists to disk and fires listeners."""
        self._events.append(event)
        if self._persist_to is not None:
            with self._persist_to.open("a", encoding="utf-8") as f:
                f.write(event.model_dump_json())
                f.write("\n")
        for listener in self._listeners:
            listener(event)

    def extend(self, events: Iterable[CycleEvent]) -> None:
        """Bulk-append. Each event still triggers persistence and listeners."""
        for event in events:
            self.append(event)

    def on_event(self, listener: _EventListener) -> None:
        """Register a callback fired on every appended event."""
        self._listeners.append(listener)

    def filter_by_kind(self, kind: CycleEventKind) -> tuple[CycleEvent, ...]:
        """Return all events of a single kind, oldest-first."""
        return tuple(e for e in self._events if e.kind is kind)

    def filter_by_cycle(self, cycle_id: str) -> tuple[CycleEvent, ...]:
        """Return all events for a single cycle id, oldest-first."""
        return tuple(e for e in self._events if e.cycle_id == cycle_id)

    def __len__(self) -> int:
        return len(self._events)


def load_jsonl(path: Path) -> tuple[CycleEvent, ...]:
    """Read a JSONL event file back into typed events.

    Skips blank lines silently; raises on malformed JSON or schema
    violations so corruption surfaces explicitly rather than silently
    truncating history.
    """
    if not path.is_file():
        return ()
    events: list[CycleEvent] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            events.append(CycleEvent.model_validate_json(stripped))
    return tuple(events)
