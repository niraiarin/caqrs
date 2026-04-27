"""Compose JQuantsClient daily bars into Observer's AssetSnapshot.

The Observer agent's input carries a tuple of :class:`AssetSnapshot`
records (per-asset summary stats: last close, 1-month return,
12-month return, 30-day volatility). This helper bridges the typed
JQuantsClient result into that shape so a Japan-equity Observer
cycle is one function call.

Computation conventions:

- ``last_close`` prefers ``adjusted_close`` (handles corporate
  actions); falls back to raw ``close`` if adjusted is missing.
- ``return_1m`` uses 21 trading days lookback. ``return_12m`` uses
  252 trading days. We index by position rather than calendar
  arithmetic, so the metric is robust to holidays and trading
  calendars.
- ``volatility_30d`` is the **sample** standard deviation of the
  last 30 daily simple returns (unannualized). Less than 30 history
  → ``None``.
- Insufficient history degrades the relevant metric to ``None``
  rather than zero — the Hypothesis agent should treat absence as
  "not enough data" rather than "no movement".

Failures from the JQuants client propagate as-is; the helper does
not swallow :class:`JQuantsError`.
"""

from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise
from statistics import stdev as _stdev

from caqrs.data.jquants.client import JQuantsClient
from caqrs.data.jquants.schemas import JQuantsDailyBar
from caqrs.schemas.observer import AssetSnapshot

_DEFAULT_HISTORY_DAYS = 365
_TRADING_DAYS_1M = 21
_TRADING_DAYS_12M = 252
_VOLATILITY_WINDOW = 30


async def fetch_jquants_asset_snapshot(
    *,
    client: JQuantsClient,
    code: str,
    as_of: date | None = None,
    history_days: int = _DEFAULT_HISTORY_DAYS,
) -> AssetSnapshot:
    """Fetch ~``history_days`` of daily bars and reduce them to one snapshot.

    Parameters
    ----------
    client:
        An open :class:`JQuantsClient`.
    code:
        4 or 5-digit J-Quants ticker code.
    as_of:
        Optional reference date. When provided, query bars in
        ``[as_of - history_days, as_of]``. When ``None``, the helper
        sends only the ``code`` filter and lets J-Quants return its
        full available history — querying past the subscription
        window end (the free tier has both a start and an end date)
        returns HTTP 400, so omitting bounds is the safe default.
    history_days:
        Calendar-day lookback used only when ``as_of`` is supplied.
        Default 365 gives ~252 trading days plus margin.

    Returns
    -------
    AssetSnapshot
        Typed summary suitable for ``ObserverArtifact.asset_snapshots``.
    """
    if as_of is not None:
        start = as_of - timedelta(days=history_days)
        bars = await client.daily_bars(code=code, from_date=start, to_date=as_of)
    else:
        bars = await client.daily_bars(code=code)
    if not bars:
        msg = f"J-Quants returned no daily bars for code={code!r}"
        raise ValueError(msg)

    closes = _ordered_closes(bars)
    last_close = closes[-1]

    return AssetSnapshot(
        ticker=code,
        last_close=last_close,
        return_1m=_simple_return(closes, lookback=_TRADING_DAYS_1M),
        return_12m=_simple_return(closes, lookback=_TRADING_DAYS_12M),
        volatility_30d=_realized_volatility(closes, window=_VOLATILITY_WINDOW),
        note=None,
    )


# === Internal helpers ===


def _ordered_closes(bars: Sequence[JQuantsDailyBar]) -> list[Decimal]:
    """Return non-null closes (adjusted preferred) in chronological order."""
    sorted_bars = sorted(bars, key=lambda b: b.date)
    closes: list[Decimal] = []
    for bar in sorted_bars:
        chosen = bar.adjusted_close if bar.adjusted_close is not None else bar.close
        if chosen is not None:
            closes.append(chosen)
    if not closes:
        msg = "No non-null closes in J-Quants daily bars; cannot compute snapshot"
        raise ValueError(msg)
    return closes


def _simple_return(closes: Sequence[Decimal], *, lookback: int) -> Decimal | None:
    """Simple return over ``lookback`` trading days; ``None`` if too short."""
    if len(closes) <= lookback:
        return None
    end_close = closes[-1]
    start_close = closes[-1 - lookback]
    if start_close == 0:
        return None
    return (end_close - start_close) / start_close


def _realized_volatility(
    closes: Sequence[Decimal],
    *,
    window: int,
) -> Decimal | None:
    """Sample stdev of the last ``window`` daily simple returns.

    Less than ``window + 1`` closes -> ``None`` (need ``window``
    consecutive return observations).
    """
    if len(closes) <= window:
        return None
    relevant = closes[-(window + 1) :]
    returns: list[float] = []
    for prev, curr in pairwise(relevant):
        if prev == 0:
            return None
        returns.append(float((curr - prev) / prev))
    if len(returns) < 2:  # noqa: PLR2004 — stdev requires >=2 data points
        return None
    return Decimal(str(_stdev(returns)))
