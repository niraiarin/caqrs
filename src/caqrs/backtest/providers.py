"""Convenience adapters that compose the backtest engine with a data source.

The walk-forward engine in :mod:`caqrs.backtest.engine` is a pure function
over polars DataFrames: caller supplies prices and signals, engine
produces a :class:`BacktestReport`. This module wires that engine to a
concrete data source (currently J-Quants) and a baseline strategy
(buy-and-hold over the plan's universe) and exposes a one-call factory
``make_jquants_buy_and_hold_executor`` whose return value is a
:data:`BacktestExecutor` ready to plug into :class:`CycleRunner`.

Subsequent slices add momentum / mean-reversion signal templates and a
vectorbt-backed alternative engine; both compose with the
:class:`PriceProvider` abstraction here.
"""

# The canonical async function shape expected by CycleRunner.
# Re-export to avoid leaking the orchestrator dependency through
# caqrs.backtest's import surface.
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, time, timedelta
from datetime import date as _date
from decimal import Decimal
from typing import Protocol

import polars as pl

from caqrs.backtest.engine import run_walk_forward
from caqrs.data.jquants import JQuantsClient
from caqrs.schemas.backtest_report import BacktestReport
from caqrs.schemas.research_plan import ResearchPlan

BacktestExecutor = Callable[[ResearchPlan], Awaitable[BacktestReport]]

_DEFAULT_NOTIONAL_USD = Decimal("1000000")


class PriceProvider(Protocol):
    """Async function shape: fetch close prices for a universe + window.

    Implementations return a long-form polars DataFrame with columns
    ``date`` (tz-aware UTC), ``ticker`` (str), ``close`` (float).
    """

    async def __call__(
        self,
        *,
        universe: Sequence[str],
        start: _date,
        end: _date,
    ) -> pl.DataFrame: ...


class JQuantsPriceProvider:
    """:class:`PriceProvider` backed by :class:`JQuantsClient`.

    Fetches daily bars for each ticker in the universe over
    ``[start, end]`` (inclusive), prefers ``adjusted_close`` for
    corporate-action handling, and skips bars whose close is null.
    Date values are promoted to tz-aware UTC datetimes (midnight) so
    they compare directly against :class:`WalkForwardWindow` bounds.
    """

    def __init__(self, *, client: JQuantsClient) -> None:
        self._client = client

    async def __call__(
        self,
        *,
        universe: Sequence[str],
        start: _date,
        end: _date,
    ) -> pl.DataFrame:
        rows: list[dict[str, object]] = []
        for code in universe:
            bars = await self._client.daily_bars(
                code=code,
                from_date=start,
                to_date=end,
            )
            for bar in bars:
                close = bar.adjusted_close if bar.adjusted_close is not None else bar.close
                if close is None:
                    continue
                rows.append(
                    {
                        "date": datetime.combine(bar.date, time(), tzinfo=UTC),
                        "ticker": code,
                        "close": float(close),
                    },
                )
        if not rows:
            return pl.DataFrame(
                schema={
                    "date": pl.Datetime(time_zone="UTC"),
                    "ticker": pl.Utf8,
                    "close": pl.Float64,
                },
            )
        return pl.DataFrame(rows)


def buy_and_hold_signals(
    *,
    plan: ResearchPlan,
    prices: pl.DataFrame,
) -> pl.DataFrame:
    """Equal-weight buy-and-hold over ``plan.universe``.

    For every distinct date in ``prices``, emit one row per ticker with
    ``weight = 1 / len(universe)``. The signal is constant through the
    test window, so after the engine's one-day lag the position is
    held from day 2 onward.
    """
    universe = list(plan.universe)
    n = len(universe)
    weight = 1.0 / n if n > 0 else 0.0
    distinct_dates = prices["date"].unique().sort().to_list()
    rows: list[dict[str, object]] = [
        {"date": d, "ticker": ticker, "weight": weight}
        for d in distinct_dates
        for ticker in universe
    ]
    if not rows:
        return pl.DataFrame(
            schema={
                "date": pl.Datetime(time_zone="UTC"),
                "ticker": pl.Utf8,
                "weight": pl.Float64,
            },
        )
    return pl.DataFrame(rows)


def make_jquants_buy_and_hold_executor(
    *,
    client: JQuantsClient,
    notional_usd: Decimal = _DEFAULT_NOTIONAL_USD,
) -> BacktestExecutor:
    """Return a :data:`BacktestExecutor` that runs an equal-weight
    buy-and-hold backtest using J-Quants prices.

    Plug this into :class:`CycleRunner` directly:

    .. code-block:: python

        executor = make_jquants_buy_and_hold_executor(client=jquants_client)
        runner = CycleRunner(..., backtest_executor=executor, ...)

    The executor fetches the union price range across all folds in one
    bulk request per ticker (rather than one fetch per fold) so the
    rate-limited free tier is respected and the engine sees consistent
    series across folds.
    """
    price_provider = JQuantsPriceProvider(client=client)

    async def _executor(plan: ResearchPlan) -> BacktestReport:
        start = min(w.test_start for w in plan.walk_forward).date()
        end = max(w.test_end for w in plan.walk_forward).date()
        prices = await price_provider(
            universe=list(plan.universe),
            start=start,
            end=end,
        )
        signals = buy_and_hold_signals(plan=plan, prices=prices)
        return run_walk_forward(
            plan=plan,
            prices=prices,
            signals=signals,
            notional_usd=notional_usd,
        )

    return _executor


def momentum_signals(
    *,
    plan: ResearchPlan,
    prices: pl.DataFrame,
    lookback_days: int,
    top_k: int | None = None,
) -> pl.DataFrame:
    """Cross-sectional momentum: rank by ``lookback_days`` past return,
    long-equally-weight the top ``top_k`` tickers.

    For each date with at least ``lookback_days`` of prior history,
    compute each ticker's return relative to ``lookback_days`` rows
    earlier and assign weight ``1 / top_k`` to the highest-ranked
    ``top_k`` tickers. Days without enough history get zero weights
    on every ticker (no position).

    ``top_k=None`` defaults to ``len(plan.universe)``, in which case
    every ticker is selected and the strategy degenerates to equal-
    weight buy-and-hold rebalanced daily.
    """
    if lookback_days <= 0:
        msg = f"lookback_days must be positive; got {lookback_days}"
        raise ValueError(msg)

    universe = list(plan.universe)
    n = len(universe)
    selected_k = top_k if top_k is not None else n
    if selected_k > n:
        msg = f"top_k={top_k} exceeds universe size {n}"
        raise ValueError(msg)
    if selected_k <= 0:
        msg = f"top_k must be positive when set; got {top_k}"
        raise ValueError(msg)

    # Build a dense {date -> {ticker -> close}} table indexed by sorted dates.
    sorted_dates = sorted(set(prices["date"].to_list()))
    close_by_date: dict[object, dict[str, float]] = {d: {} for d in sorted_dates}
    for date_value, ticker, close in zip(
        prices["date"].to_list(),
        prices["ticker"].to_list(),
        prices["close"].to_list(),
        strict=True,
    ):
        close_by_date[date_value][str(ticker)] = float(close)

    weight = 1.0 / float(selected_k) if selected_k > 0 else 0.0
    out_rows: list[dict[str, object]] = []
    for i, day in enumerate(sorted_dates):
        if i < lookback_days:
            out_rows.extend(
                {"date": day, "ticker": ticker, "weight": 0.0} for ticker in universe
            )
            continue
        prior_day = sorted_dates[i - lookback_days]
        prior_closes = close_by_date.get(prior_day, {})
        current_closes = close_by_date.get(day, {})
        ranked: list[tuple[str, float]] = [
            (
                ticker,
                ((current_closes[ticker] - prior_closes[ticker]) / prior_closes[ticker])
                if (
                    ticker in current_closes
                    and ticker in prior_closes
                    and prior_closes[ticker] != 0
                )
                else float("-inf"),
            )
            for ticker in universe
        ]
        ranked.sort(key=lambda pair: pair[1], reverse=True)
        winners = {t for t, _ in ranked[:selected_k]}
        out_rows.extend(
            {
                "date": day,
                "ticker": ticker,
                "weight": weight if ticker in winners else 0.0,
            }
            for ticker in universe
        )

    if not out_rows:
        return pl.DataFrame(
            schema={
                "date": pl.Datetime(time_zone="UTC"),
                "ticker": pl.Utf8,
                "weight": pl.Float64,
            },
        )
    return pl.DataFrame(out_rows)


def make_jquants_momentum_executor(
    *,
    client: JQuantsClient,
    lookback_days: int,
    top_k: int | None = None,
    notional_usd: Decimal = _DEFAULT_NOTIONAL_USD,
) -> BacktestExecutor:
    """Return a :data:`BacktestExecutor` that runs a top-K momentum
    strategy over ``plan.universe`` using J-Quants prices.

    Pre-fetches a calendar-day buffer of ``2 * lookback_days`` before
    the earliest ``test_start`` so day-1 of the test window already
    has a computable lookback return (J-Quants prices are weekly
    sparse — weekends and holidays — so we over-allocate the buffer).

    See :func:`momentum_signals` for the strategy semantics.
    """
    if lookback_days <= 0:
        msg = f"lookback_days must be positive; got {lookback_days}"
        raise ValueError(msg)

    price_provider = JQuantsPriceProvider(client=client)
    buffer_days = max(lookback_days * 2, lookback_days + 7)

    async def _executor(plan: ResearchPlan) -> BacktestReport:
        first_test_start = min(w.test_start for w in plan.walk_forward).date()
        last_test_end = max(w.test_end for w in plan.walk_forward).date()
        fetch_start = first_test_start - timedelta(days=buffer_days)
        prices = await price_provider(
            universe=list(plan.universe),
            start=fetch_start,
            end=last_test_end,
        )
        signals = momentum_signals(
            plan=plan,
            prices=prices,
            lookback_days=lookback_days,
            top_k=top_k,
        )
        return run_walk_forward(
            plan=plan,
            prices=prices,
            signals=signals,
            notional_usd=notional_usd,
        )

    return _executor
