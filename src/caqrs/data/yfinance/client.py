"""Async wrapper over the sync ``yfinance`` library.

The client encodes the production lessons from the Zenn yfinance-
production-pitfalls + rakuscan-data-layer-pipeline articles:

- ``auto_adjust=True`` on every history call (split discontinuities).
- Per-process ``tempfile.mkdtemp`` for the tz cache (avoids
  concurrent-process SQLite contention on ``~/.cache/py-yfinance``).
- Throttle interval between successive calls (default 1.0s).
- Treat 3 consecutive empty responses as quota exhaustion (the
  ``_quota_exhausted`` heuristic from the rakuscan article).
- NaN-safe coercion: any NaN read from the DataFrame becomes
  ``None`` before reaching the typed schema.

The client does **not** retry by itself on rate-limit signals; a
caller (CycleRunner / supervisor) decides whether to back off and
when. Linear backoff (5/10/15s) is recommended in the rakuscan
article and can be applied at the call site.
"""

from __future__ import annotations

import asyncio
import atexit
import math
import shutil
import tempfile
import time
from datetime import date as _date
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Any

import yfinance as yf

from caqrs.data.yfinance.schemas import YFinancePrice

_DEFAULT_THROTTLE_SECONDS = 1.0
_QUOTA_EXHAUSTED_AFTER_CONSECUTIVE_EMPTIES = 3


class YFinanceError(Exception):
    """Base class for typed yFinance failures the caller can match on."""


class YFinanceQuotaExhaustedError(YFinanceError):
    """Three consecutive empty responses → upstream is rate-limiting us.

    Distinct from ``YFinanceError`` so callers can apply a longer
    backoff (e.g. minutes) for this case while still retrying on
    transient failures.
    """


class YFinanceClient:
    """Async-friendly facade over the sync ``yfinance`` library.

    Construct as a context manager so the per-process tz cache is
    cleaned up:

    .. code-block:: python

        async with YFinanceClient() as client:
            bars = await client.daily_bars(
                symbol="AAPL",
                from_date=date(2026, 4, 1),
                to_date=date(2026, 4, 28),
            )
    """

    def __init__(
        self,
        *,
        throttle_seconds: float = _DEFAULT_THROTTLE_SECONDS,
    ) -> None:
        self._throttle = throttle_seconds
        self._last_call_at: float = 0.0
        self._consecutive_empties = 0

        # Per-process tz cache to avoid SQLite contention. Cleaned up
        # on aclose() / __aexit__. atexit fallback covers the case of
        # the user forgetting to close.
        self._tz_cache_dir: Path | None = Path(tempfile.mkdtemp(prefix="caqrs-yf-"))
        yf.set_tz_cache_location(str(self._tz_cache_dir))
        atexit.register(self._cleanup_tz_cache)

    @property
    def tz_cache_dir(self) -> Path | None:
        """Directory where yfinance caches tz lookups for this client.

        ``None`` after :meth:`aclose` has been called.
        """
        return self._tz_cache_dir

    async def __aenter__(self) -> YFinanceClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Remove the per-process tz cache directory."""
        self._cleanup_tz_cache()

    # === Public API ===

    async def daily_bars(
        self,
        *,
        symbol: str,
        from_date: _date,
        to_date: _date,
    ) -> list[YFinancePrice]:
        """Fetch daily OHLCV bars for ``[from_date, to_date]`` inclusive.

        Returns ``[]`` for a single empty response (could be "no
        trading days in range"). Raises
        :class:`YFinanceQuotaExhaustedError` after 3 consecutive
        empties — at that point the upstream is almost certainly
        rate-limiting us.
        """
        await self._throttle_sleep()
        # yfinance's `end` is exclusive; add one day so to_date is
        # included in the result.
        end_exclusive = to_date + timedelta(days=1)
        frame = await asyncio.to_thread(
            self._fetch_history,
            symbol=symbol,
            start=from_date,
            end=end_exclusive,
        )

        if frame.empty:
            self._consecutive_empties += 1
            if self._consecutive_empties >= _QUOTA_EXHAUSTED_AFTER_CONSECUTIVE_EMPTIES:
                msg = (
                    f"3 consecutive empty responses from yfinance "
                    f"(last symbol={symbol}); treating as quota exhausted"
                )
                raise YFinanceQuotaExhaustedError(msg)
            return []

        self._consecutive_empties = 0
        return [
            self._row_to_price(symbol=symbol, idx=idx, row=row) for idx, row in frame.iterrows()
        ]

    # === Internal ===

    @staticmethod
    def _fetch_history(*, symbol: str, start: _date, end: _date) -> Any:
        ticker = yf.Ticker(symbol)
        # auto_adjust=True merges adjusted-close into close so split
        # discontinuities never appear; mandatory per Zenn pitfalls.
        return ticker.history(
            start=start.isoformat(),
            end=end.isoformat(),
            auto_adjust=True,
            actions=False,
        )

    async def _throttle_sleep(self) -> None:
        if self._throttle <= 0:
            return
        elapsed = time.monotonic() - self._last_call_at
        wait = self._throttle - elapsed
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call_at = time.monotonic()

    @staticmethod
    def _row_to_price(*, symbol: str, idx: Any, row: Any) -> YFinancePrice:
        return YFinancePrice(
            symbol=symbol,
            date=idx.date() if hasattr(idx, "date") else _date.fromisoformat(str(idx)[:10]),
            open=Decimal(str(row["Open"])),
            high=Decimal(str(row["High"])),
            low=Decimal(str(row["Low"])),
            close=Decimal(str(row["Close"])),
            # auto_adjust=True merges adjusted into close — leave the
            # explicit field None.
            adjusted_close=None,
            volume=_nan_safe_int(row.get("Volume")),
        )

    def _cleanup_tz_cache(self) -> None:
        if self._tz_cache_dir is not None and self._tz_cache_dir.exists():
            shutil.rmtree(self._tz_cache_dir, ignore_errors=True)
        self._tz_cache_dir = None


def _nan_safe_int(value: Any) -> int | None:
    """Convert a possibly-NaN pandas value to ``int | None``."""
    if value is None:
        return None
    try:
        as_float = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(as_float):
        return None
    return int(as_float)
