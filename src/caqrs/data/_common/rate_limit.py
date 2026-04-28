"""Async-friendly rate limiter shared across data clients.

Two responsibilities:

1. **Min-interval pacing** — :meth:`acquire` waits as much as
   needed so consecutive calls are at least ``min_interval_seconds``
   apart. The caller calls it before every upstream request.
2. **429 backoff tracking** — :meth:`on_429` records a rate-limited
   response and returns the appropriate sleep duration from the
   configured ``retry_schedule``. :meth:`reset_429` clears the
   counter on the first success after a streak.

The defaults follow the Zenn ino_h article's J-Quants observation
that **linear** backoff (30/60/90s) outperforms exponential because
the upstream rate-limit window is fixed at 1 minute. Other clients
override per their published limits:

- J-Quants free tier: 5 req/min → ``min_interval_seconds=12.0``,
  retry 30/60/90.
- yfinance: per-call only; the dedicated quota detector handles
  the empty-response signal separately.
- Polymarket public APIs: ~10 req/sec → ``min_interval_seconds=0.1``.
- EDINET: no published limit; conservative ``0.1``.
"""

from __future__ import annotations

import asyncio
import time

# Linear backoff per Zenn ino_h article: 30/60/90 seconds for J-Quants
# (the API resets its budget every minute, so exponentially short
# waits like 5/10/15s don't outlast the rate-limit window).
_DEFAULT_RETRY_SCHEDULE: tuple[int, ...] = (30, 60, 90)


class AsyncRateLimiter:
    """Coordinates a single upstream's request cadence.

    Construct one limiter per upstream service (J-Quants, Polymarket
    CLOB, etc.) and inject it into every client that talks to that
    service so multiple clients in the same process share the timing
    clock. The limiter is **not** thread-safe; callers must use it
    from a single asyncio event loop.
    """

    def __init__(
        self,
        *,
        min_interval_seconds: float,
        retry_schedule: tuple[int, ...] = _DEFAULT_RETRY_SCHEDULE,
    ) -> None:
        if min_interval_seconds < 0:
            msg = f"min_interval_seconds must be non-negative; got {min_interval_seconds}"
            raise ValueError(msg)
        if not retry_schedule:
            msg = "retry_schedule must contain at least one entry"
            raise ValueError(msg)
        self._min_interval = min_interval_seconds
        self._retry_schedule = retry_schedule
        self._last_acquire_at: float = 0.0
        self._consecutive_429s = 0

    @property
    def min_interval_seconds(self) -> float:
        return self._min_interval

    @property
    def retry_schedule(self) -> tuple[int, ...]:
        return self._retry_schedule

    async def acquire(self) -> None:
        """Wait until ``min_interval_seconds`` has elapsed since the
        previous call, then record this acquire's timestamp.

        Zero or negative wait → no sleep. The first ever acquire
        never sleeps because ``_last_acquire_at`` starts at 0.
        """
        if self._min_interval <= 0:
            self._last_acquire_at = time.monotonic()
            return
        elapsed = time.monotonic() - self._last_acquire_at
        wait = self._min_interval - elapsed
        if wait > 0 and self._last_acquire_at > 0:
            await asyncio.sleep(wait)
        self._last_acquire_at = time.monotonic()

    def on_429(self) -> int:
        """Record a 429 response and return the next backoff duration.

        After the schedule is exhausted (more 429s than entries), the
        last value is repeated — graceful degradation in the face of
        a sustained outage rather than raising.
        """
        idx = min(self._consecutive_429s, len(self._retry_schedule) - 1)
        self._consecutive_429s += 1
        return self._retry_schedule[idx]

    def reset_429(self) -> None:
        """Clear the 429 counter — call after the first successful
        response following a rate-limit streak so the next 429
        starts the schedule over."""
        self._consecutive_429s = 0
