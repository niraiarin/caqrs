"""Interval-based fire tracker for unattended cycle dispatch.

A :class:`Heartbeat` is a pure object: caller polls
:meth:`is_due` and calls :meth:`fire` after enqueuing a cycle. No
threads, no asyncio, no cron parsing — the surrounding event loop
owns timing. This keeps the scheduler trivially testable (inject
a deterministic clock) and avoids dragging in a cron dependency for
the simple "every N minutes" case the early CAQRS deployments
need.

Composes naturally with :class:`CycleQueue`::

    while True:
        if heartbeat.is_due():
            queue.enqueue(build_observer_input())
            heartbeat.fire()
        await queue.run_one()
        await asyncio.sleep(0.5)

For real cron expressions ("weekdays at 16:30 ET") we will add a
separate ``CronSchedule`` adapter when needed; the queue / runner
layer below stays unchanged.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Heartbeat:
    """Tracks fire times against a fixed interval.

    Parameters
    ----------
    interval:
        Minimum gap between fires. Must be strictly positive.
    clock:
        Optional injectable clock. Default reads ``datetime.now(UTC)``.
        Used for deterministic tests.
    """

    def __init__(
        self,
        *,
        interval: timedelta,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if interval <= timedelta(0):
            msg = f"interval must be positive; got {interval}"
            raise ValueError(msg)
        self._interval = interval
        self._clock = clock or _utc_now
        self._last_fired_at: datetime | None = None

    @property
    def interval(self) -> timedelta:
        return self._interval

    @property
    def last_fired_at(self) -> datetime | None:
        return self._last_fired_at

    def is_due(self) -> bool:
        """True iff at least ``interval`` has elapsed since the last fire,
        or no fire has happened yet."""
        if self._last_fired_at is None:
            return True
        return self._clock() - self._last_fired_at >= self._interval

    def fire(self) -> None:
        """Mark a fire at the current clock value."""
        self._last_fired_at = self._clock()
