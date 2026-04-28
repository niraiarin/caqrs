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
from datetime import UTC, datetime, time
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
