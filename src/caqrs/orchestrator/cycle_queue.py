"""Serial cycle dispatcher with reentrancy guard.

Multiple :class:`ObserverInput` requests may be enqueued from
concurrent callers, but only one cycle runs at a time — the
research pipeline is intentionally serial. This mirrors Mercury's
``processQueue`` pattern (survey item 13) where the harness picks
items off a single queue and runs them through the agentic loop one
at a time.

Why serial?

- LLM cost and rate-limit pressure: a research cycle calls 5+ agents,
  each emitting ≥1 LLM call. Letting them race multiplies failure
  modes that the loop detector / budget guard cannot easily reason
  about.
- Deterministic event ordering: the EventLog is append-only and
  shared across cycles; serial dispatch preserves a clean per-cycle
  ordering without locks per event.
- Cost predictability: with a per-cycle :class:`CycleBudget`, serial
  dispatch lets each cycle exhaust its cap before the next starts.

Parallel exploration is opt-in and lives outside this queue.
"""

from collections import deque

from caqrs.orchestrator.cycle_runner import CycleResult, CycleRunner
from caqrs.schemas.observer import ObserverInput


class CycleQueue:
    """FIFO queue of pending cycles; serial dispatch via the runner."""

    def __init__(self, *, runner: CycleRunner) -> None:
        self._runner = runner
        self._pending: deque[ObserverInput] = deque()
        self._running = False

    @property
    def pending(self) -> int:
        return len(self._pending)

    @property
    def is_running(self) -> bool:
        return self._running

    def enqueue(self, observer_input: ObserverInput) -> None:
        """Append an :class:`ObserverInput` to the queue. Cheap and
        safe to call from any caller, sync or async."""
        self._pending.append(observer_input)

    async def run_one(self) -> CycleResult | None:
        """Pop one item and run it through the runner.

        Returns ``None`` if the queue is empty or another caller is
        already running a cycle (reentrancy guard).
        """
        if self._running or not self._pending:
            return None
        observer_input = self._pending.popleft()
        return await self._run_guarded(observer_input)

    async def drain(self) -> tuple[CycleResult, ...]:
        """Run every queued cycle in FIFO order and return all results.

        Concurrent ``drain`` calls are safe: the second one observes
        the reentrancy guard and returns an empty tuple. The first
        caller does the work.
        """
        if self._running:
            return ()
        results: list[CycleResult] = []
        while self._pending:
            observer_input = self._pending.popleft()
            results.append(await self._run_guarded(observer_input))
        return tuple(results)

    async def _run_guarded(self, observer_input: ObserverInput) -> CycleResult:
        self._running = True
        try:
            return await self._runner.run(observer_input)
        finally:
            self._running = False
