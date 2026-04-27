"""Walk-forward backtest core.

Pure function over polars DataFrames:

    run_walk_forward(plan, prices, signals, notional_usd) -> BacktestReport

Inputs
------
- ``prices``: long-form ``pl.DataFrame[date, ticker, close]``.
- ``signals``: long-form ``pl.DataFrame[date, ticker, weight]``. ``weight``
  is the desired position weight per ticker per date. Sum of weights per
  date should be ≤ 1 (cash-only baseline) but the engine does not enforce
  that — the Policy Gateway (P3) is the layer that fail-stops violations.

Per-fold computation
--------------------
For each :class:`WalkForwardWindow` in ``plan.walk_forward``:

1. Filter prices + signals to the test window ``[test_start, test_end]``.
2. Pivot to wide form (rows = date, cols = ticker).
3. Daily simple returns from price changes.
4. **Lag signals by one day** to avoid lookahead bias: weight observed
   at end of day ``t`` is held over day ``t+1``.
5. Daily portfolio return = sum over tickers of ``lagged_weight x return``.
6. Apply transaction costs on weight changes:
   ``cost = (cost_bps + slippage_bps) / 10000 x |dweight|`` per ticker
   per day, subtracted from that day's portfolio return.
7. Equity curve = cumulative product of ``(1 + portfolio_return)``.
8. Metrics:
   - **Sharpe** = ``mean / stdev x sqrt(252)``; defined as 0 when stdev is 0.
   - **Max drawdown** = peak-to-trough decline of equity, in percent.
   - **Turnover** = sum of ``|dweight|`` over (date, ticker) divided by N
     days.
   - **n_trades** = count of (date, ticker) where weight changed.
   - **PnL USD** = ``notional x (final_equity - 1)``.

Aggregate
---------
- ``median_sharpe`` / ``mean_sharpe`` / ``worst_fold_sharpe`` over folds.
- ``median_max_drawdown_pct`` over folds.
- ``total_pnl_usd`` = sum of fold PnLs.
- ``total_trades`` = sum of fold trade counts.

Edge cases the engine asserts on
--------------------------------
- A signal references a ticker not present in ``prices`` → ``ValueError``.
- A fold's test window contains no price rows → ``ValueError``.
"""

from datetime import UTC, datetime
from decimal import Decimal
from math import sqrt
from statistics import mean, median, stdev

import polars as pl

from caqrs.schemas.backtest_report import (
    AggregateMetrics,
    BacktestReport,
    FoldMetrics,
)
from caqrs.schemas.common import RunMetadata, new_run_id
from caqrs.schemas.research_plan import ResearchPlan, WalkForwardWindow

_DEFAULT_NOTIONAL = Decimal("1000000")
_TRADING_DAYS_PER_YEAR = 252
_BPS_DENOMINATOR = Decimal("10000")
_TRADE_THRESHOLD = 1e-12  # weight changes below this are ignored


def run_walk_forward(
    *,
    plan: ResearchPlan,
    prices: pl.DataFrame,
    signals: pl.DataFrame,
    notional_usd: Decimal = _DEFAULT_NOTIONAL,
) -> BacktestReport:
    """Run the walk-forward backtest defined by ``plan``.

    See module docstring for inputs / metrics / edge cases.
    """
    _validate_universe(prices=prices, signals=signals)

    folds = tuple(
        _run_fold(
            fold_index=i,
            window=window,
            prices=prices,
            signals=signals,
            cost_bps=plan.cost_model_bps,
            slippage_bps=plan.slippage_bps,
            notional_usd=notional_usd,
        )
        for i, window in enumerate(plan.walk_forward)
    )
    return BacktestReport(
        metadata=_build_run_metadata(),
        plan_run_id=plan.metadata.run_id,
        folds=folds,
        aggregate=_aggregate(folds),
    )


# === Per-fold computation ===


def _run_fold(
    *,
    fold_index: int,
    window: WalkForwardWindow,
    prices: pl.DataFrame,
    signals: pl.DataFrame,
    cost_bps: Decimal,
    slippage_bps: Decimal,
    notional_usd: Decimal,
) -> FoldMetrics:
    fold_prices = _filter_to_window(prices, start=window.test_start, end=window.test_end)
    if fold_prices.height == 0:
        msg = (
            f"backtest fold {fold_index}: no prices in test window "
            f"[{window.test_start.isoformat()}, {window.test_end.isoformat()}]"
        )
        raise ValueError(msg)
    fold_signals = _filter_to_window(signals, start=window.test_start, end=window.test_end)

    price_wide = _pivot(fold_prices, value_col="close")
    signal_wide = _pivot(fold_signals, value_col="weight")
    aligned_signals = _align_columns(price_wide=price_wide, signal_wide=signal_wide)

    # Daily simple returns (date col preserved at index 0)
    returns_wide = _daily_returns(price_wide)

    # Lag signals by 1 day so weight observed at t is held over t+1
    lagged_signals = _shift_one_day(aligned_signals)

    # Portfolio daily return per day (length N; day-1 entry is 0 by lag)
    portfolio_returns = _portfolio_returns(returns_wide, lagged_signals)

    # Transaction costs per day (length N; day-1 cost = entry rebalancing)
    cost_per_day = _cost_series(
        signal_wide=aligned_signals,
        cost_bps=cost_bps,
        slippage_bps=slippage_bps,
    )

    # Day 1 has no measurable return (no prior price). Drop it. Costs
    # incurred at end of day t-1 are paid out of day t's net return,
    # so cost[t-1] is paired with portfolio_returns[t]. This is the
    # standard rebalancing-at-close, return-realised-next-day convention.
    if len(portfolio_returns) <= 1:
        net_returns: list[float] = []
    else:
        net_returns = [
            portfolio_returns[i] - cost_per_day[i - 1] for i in range(1, len(portfolio_returns))
        ]

    sharpe = _sharpe(net_returns)
    max_drawdown_pct = _max_drawdown_pct(net_returns)
    turnover = _turnover(aligned_signals)
    n_trades = _n_trades(aligned_signals)
    pnl_usd = _pnl(net_returns, notional_usd=notional_usd)

    return FoldMetrics(
        fold_index=fold_index,
        test_start=window.test_start,
        test_end=window.test_end,
        sharpe=sharpe,
        max_drawdown_pct=max_drawdown_pct,
        turnover=turnover,
        n_trades=n_trades,
        pnl_usd=pnl_usd,
    )


# === Aggregation ===


def _aggregate(folds: tuple[FoldMetrics, ...]) -> AggregateMetrics:
    if not folds:
        msg = "BacktestReport requires at least one fold; got zero."
        raise ValueError(msg)
    sharpes = [float(f.sharpe) for f in folds]
    drawdowns = [float(f.max_drawdown_pct) for f in folds]
    return AggregateMetrics(
        median_sharpe=Decimal(str(median(sharpes))),
        mean_sharpe=Decimal(str(mean(sharpes))),
        worst_fold_sharpe=Decimal(str(min(sharpes))),
        median_max_drawdown_pct=Decimal(str(median(drawdowns))),
        total_pnl_usd=sum((f.pnl_usd for f in folds), start=Decimal(0)),
        total_trades=sum(f.n_trades for f in folds),
    )


# === Validation ===


def _validate_universe(*, prices: pl.DataFrame, signals: pl.DataFrame) -> None:
    price_tickers = set(prices["ticker"].unique().to_list())
    signal_tickers = set(signals["ticker"].unique().to_list())
    missing = signal_tickers - price_tickers
    if missing:
        msg = f"signals reference ticker(s) not in prices: {sorted(missing)}"
        raise ValueError(msg)


# === DataFrame helpers (polars) ===


def _filter_to_window(df: pl.DataFrame, *, start: datetime, end: datetime) -> pl.DataFrame:
    return df.filter(pl.col("date").is_between(start, end, closed="both"))


def _pivot(df: pl.DataFrame, *, value_col: str) -> pl.DataFrame:
    return df.pivot(index="date", on="ticker", values=value_col).sort("date")


def _align_columns(
    *,
    price_wide: pl.DataFrame,
    signal_wide: pl.DataFrame,
) -> pl.DataFrame:
    """Reindex signal_wide to match price_wide's date axis and column order.

    Missing (date, ticker) signal cells become 0 (no position).
    """
    ticker_cols = [c for c in price_wide.columns if c != "date"]
    aligned = price_wide.select(["date"]).join(signal_wide, on="date", how="left")
    for col in ticker_cols:
        if col not in aligned.columns:
            aligned = aligned.with_columns(pl.lit(0.0).alias(col))
    aligned = aligned.with_columns(
        [pl.col(c).fill_null(0.0).cast(pl.Float64) for c in ticker_cols],
    )
    # Drop any stray ticker columns that came from signal_wide but are not
    # in the price universe — those would be a programming error caught
    # earlier by _validate_universe; defensive.
    return aligned.select(["date", *ticker_cols])


def _daily_returns(price_wide: pl.DataFrame) -> pl.DataFrame:
    """Pct-change of every ticker column. First row's returns are 0."""
    ticker_cols = [c for c in price_wide.columns if c != "date"]
    return price_wide.with_columns(
        [pl.col(c).pct_change().fill_null(0.0).alias(c) for c in ticker_cols],
    )


def _shift_one_day(df: pl.DataFrame) -> pl.DataFrame:
    """Shift every non-date column down by one row (lag by one day).

    The first row becomes 0 (no position before the test window starts).
    """
    ticker_cols = [c for c in df.columns if c != "date"]
    return df.with_columns(
        [pl.col(c).shift(1).fill_null(0.0).alias(c) for c in ticker_cols],
    )


def _portfolio_returns(
    returns_wide: pl.DataFrame,
    lagged_signals: pl.DataFrame,
) -> list[float]:
    ticker_cols = [c for c in returns_wide.columns if c != "date"]
    rows: list[float] = []
    # Iterate row-pairs; not the fastest but tractable at typical fold sizes
    for r_row, s_row in zip(
        returns_wide.iter_rows(named=True),
        lagged_signals.iter_rows(named=True),
        strict=True,
    ):
        total = 0.0
        for c in ticker_cols:
            total += float(r_row[c]) * float(s_row[c])
        rows.append(total)
    return rows


def _cost_series(
    *,
    signal_wide: pl.DataFrame,
    cost_bps: Decimal,
    slippage_bps: Decimal,
) -> list[float]:
    """Per-day transaction cost as a fraction of equity (subtracted from return).

    cost_t = ((cost_bps + slippage_bps) / 10_000) x sum_ticker |dweight_t|
    dweight on day 1 is the absolute weight (we're entering from a flat
    book).
    """
    total_bps = float(cost_bps + slippage_bps)
    rate = total_bps / float(_BPS_DENOMINATOR)
    ticker_cols = [c for c in signal_wide.columns if c != "date"]
    rows: list[float] = []
    prev: dict[str, float] = dict.fromkeys(ticker_cols, 0.0)
    for s_row in signal_wide.iter_rows(named=True):
        delta_total = 0.0
        for c in ticker_cols:
            curr = float(s_row[c])
            delta_total += abs(curr - prev[c])
            prev[c] = curr
        rows.append(rate * delta_total)
    return rows


# === Metric primitives ===


_SHARPE_STDEV_EPS = 1e-12  # treat near-zero stdev (constant returns + FP noise) as zero


def _sharpe(returns: list[float]) -> Decimal:
    if not returns:
        return Decimal(0)
    if len(returns) < 2:  # noqa: PLR2004 — stdev needs >=2
        return Decimal(0)
    sigma = stdev(returns)
    if sigma < _SHARPE_STDEV_EPS:
        return Decimal(0)
    annualised = mean(returns) / sigma * sqrt(_TRADING_DAYS_PER_YEAR)
    return Decimal(str(annualised))


def _max_drawdown_pct(returns: list[float]) -> Decimal:
    if not returns:
        return Decimal(0)
    equity = 1.0
    peak = 1.0
    worst = 0.0
    for r in returns:
        equity *= 1.0 + r
        peak = max(peak, equity)
        if peak > 0:
            dd = (peak - equity) / peak
            worst = max(worst, dd)
    return Decimal(str(worst * 100.0))


def _turnover(signal_wide: pl.DataFrame) -> Decimal:
    ticker_cols = [c for c in signal_wide.columns if c != "date"]
    if signal_wide.height == 0:
        return Decimal(0)
    total = 0.0
    prev: dict[str, float] = dict.fromkeys(ticker_cols, 0.0)
    for s_row in signal_wide.iter_rows(named=True):
        for c in ticker_cols:
            curr = float(s_row[c])
            total += abs(curr - prev[c])
            prev[c] = curr
    return Decimal(str(total / float(signal_wide.height)))


def _n_trades(signal_wide: pl.DataFrame) -> int:
    ticker_cols = [c for c in signal_wide.columns if c != "date"]
    n = 0
    prev: dict[str, float] = dict.fromkeys(ticker_cols, 0.0)
    for s_row in signal_wide.iter_rows(named=True):
        for c in ticker_cols:
            curr = float(s_row[c])
            if abs(curr - prev[c]) > _TRADE_THRESHOLD:
                n += 1
            prev[c] = curr
    return n


def _pnl(returns: list[float], *, notional_usd: Decimal) -> Decimal:
    equity = 1.0
    for r in returns:
        equity *= 1.0 + r
    return notional_usd * Decimal(str(equity - 1.0))


def _build_run_metadata() -> RunMetadata:
    return RunMetadata(
        run_id=new_run_id(),
        parent_id=None,
        agent_name="backtest_engine",
        model_id="caqrs.backtest.run_walk_forward",
        created_at=datetime.now(UTC),
        llm_cost_usd=Decimal(0),
        latency_ms=0,
        token_in=0,
        token_out=0,
    )
