"""yFinance → AssetSnapshot bridge.

Mirrors :mod:`caqrs.data.jquants.observer_signals` exactly: same
metric definitions (21-day return, 252-day return, 30-day sample
stdev), same "insufficient history → ``None``" semantics. The agent
receives an :class:`AssetSnapshot` and can't tell whether yfinance
or J-Quants produced it — that's the whole point of routing both
through the same shape.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise
from statistics import stdev as _stdev

from caqrs.data.yfinance.client import YFinanceClient
from caqrs.data.yfinance.schemas import YFinancePrice
from caqrs.schemas.observer import AssetSnapshot

_DEFAULT_HISTORY_DAYS = 365
_TRADING_DAYS_1M = 21
_TRADING_DAYS_12M = 252
_VOLATILITY_WINDOW = 30


async def fetch_yfinance_asset_snapshot(
    *,
    client: YFinanceClient,
    symbol: str,
    as_of: date | None = None,
    history_days: int = _DEFAULT_HISTORY_DAYS,
) -> AssetSnapshot:
    """Fetch ~``history_days`` of daily bars and reduce them to one snapshot.

    Same parameter / return semantics as
    :func:`caqrs.data.jquants.observer_signals.fetch_jquants_asset_snapshot`
    so the two are interchangeable behind a fallback chain
    (``yfinance → J-Quants`` for a Japan listing, etc.).
    """
    today = as_of or date.today()
    from_date = today - timedelta(days=history_days)
    bars = await client.daily_bars(
        symbol=symbol,
        from_date=from_date,
        to_date=today,
    )
    return _reduce_to_snapshot(symbol=symbol, bars=bars)


def _reduce_to_snapshot(
    *,
    symbol: str,
    bars: list[YFinancePrice],
) -> AssetSnapshot:
    if not bars:
        return AssetSnapshot(
            ticker=symbol,
            last_close=Decimal(0),
            return_1m=None,
            return_12m=None,
            volatility_30d=None,
        )

    # Sorted ascending by date — yfinance returns this shape natively
    # but explicit sort is a cheap safety net.
    bars_sorted = sorted(bars, key=lambda b: b.date)
    closes = [_close_of(b) for b in bars_sorted]
    last_close = closes[-1]

    return AssetSnapshot(
        ticker=symbol,
        last_close=last_close,
        return_1m=_position_return(closes=closes, lookback=_TRADING_DAYS_1M),
        return_12m=_position_return(closes=closes, lookback=_TRADING_DAYS_12M),
        volatility_30d=_recent_stdev(closes=closes, window=_VOLATILITY_WINDOW),
    )


def _close_of(bar: YFinancePrice) -> Decimal:
    """Prefer adjusted_close; auto_adjust=True merges it into close so
    in practice we read close directly."""
    return bar.adjusted_close if bar.adjusted_close is not None else bar.close


def _position_return(*, closes: list[Decimal], lookback: int) -> Decimal | None:
    if len(closes) <= lookback:
        return None
    prior = closes[-lookback - 1]
    if prior <= 0:
        return None
    return (closes[-1] - prior) / prior


def _recent_stdev(*, closes: list[Decimal], window: int) -> Decimal | None:
    if len(closes) < window + 1:
        return None
    recent = closes[-(window + 1) :]
    daily_returns = [float((b - a) / a) if a > 0 else 0.0 for a, b in pairwise(recent)]
    return Decimal(str(_stdev(daily_returns)))
