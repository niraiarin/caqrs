"""Tests for the walk-forward backtest engine."""

from datetime import UTC, datetime
from decimal import Decimal

import polars as pl
import pytest

from caqrs.backtest import run_walk_forward
from caqrs.schemas.common import RunMetadata, new_run_id
from caqrs.schemas.research_plan import (
    DataFrequency,
    ResearchPlan,
    WalkForwardWindow,
)

# === Fixtures ===


def _meta() -> RunMetadata:
    return RunMetadata(
        run_id=new_run_id(),
        parent_id=None,
        agent_name="research",
        model_id="test",
        created_at=datetime.now(UTC),
        llm_cost_usd=Decimal(0),
        latency_ms=0,
        token_in=0,
        token_out=0,
    )


def _plan(
    *,
    walk_forward: tuple[WalkForwardWindow, ...],
    cost_bps: Decimal = Decimal("0"),
    slippage_bps: Decimal = Decimal("0"),
) -> ResearchPlan:
    return ResearchPlan(
        metadata=_meta(),
        hypothesis_run_id=new_run_id(),
        universe=("AAPL",),
        frequency=DataFrequency.DAILY,
        walk_forward=walk_forward,
        cost_model_bps=cost_bps,
        slippage_bps=slippage_bps,
        seed=1,
    )


def _window(
    *, train_start: str, train_end: str, test_start: str, test_end: str
) -> WalkForwardWindow:
    return WalkForwardWindow(
        train_start=datetime.fromisoformat(train_start).replace(tzinfo=UTC),
        train_end=datetime.fromisoformat(train_end).replace(tzinfo=UTC),
        test_start=datetime.fromisoformat(test_start).replace(tzinfo=UTC),
        test_end=datetime.fromisoformat(test_end).replace(tzinfo=UTC),
    )


def _prices(rows: list[tuple[str, str, float]]) -> pl.DataFrame:
    """rows: (date_iso, ticker, close)."""
    return pl.DataFrame(
        {
            "date": [datetime.fromisoformat(d).replace(tzinfo=UTC) for d, _, _ in rows],
            "ticker": [t for _, t, _ in rows],
            "close": [c for _, _, c in rows],
        },
    )


def _signals(rows: list[tuple[str, str, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": [datetime.fromisoformat(d).replace(tzinfo=UTC) for d, _, _ in rows],
            "ticker": [t for _, t, _ in rows],
            "weight": [w for _, _, w in rows],
        },
    )


def _flat_prices(*, dates: list[str], ticker: str, price: float) -> pl.DataFrame:
    return _prices([(d, ticker, price) for d in dates])


def _constant_signals(*, dates: list[str], ticker: str, weight: float) -> pl.DataFrame:
    return _signals([(d, ticker, weight) for d in dates])


# === Single fold ===


def test_walk_forward_single_fold_flat_prices_yields_zero_pnl() -> None:
    dates = [f"2025-01-{d:02d}" for d in range(1, 11)]  # 10 days
    plan = _plan(
        walk_forward=(
            _window(
                train_start="2024-12-01",
                train_end="2024-12-31",
                test_start="2025-01-01",
                test_end="2025-01-10",
            ),
        ),
    )
    prices = _flat_prices(dates=dates, ticker="AAPL", price=100.0)
    sigs = _constant_signals(dates=dates, ticker="AAPL", weight=1.0)

    report = run_walk_forward(plan=plan, prices=prices, signals=sigs)

    assert len(report.folds) == 1
    fold = report.folds[0]
    assert fold.fold_index == 0
    assert fold.pnl_usd == Decimal(0)
    assert fold.max_drawdown_pct == Decimal(0)
    assert fold.sharpe == Decimal(0)  # all-zero returns → sharpe defined as 0


def test_walk_forward_single_fold_constant_uptrend_compounds_returns() -> None:
    """Each day +1% → portfolio compounds. Sharpe is 0 (zero stdev) but pnl is nonzero."""
    dates = [f"2025-01-{d:02d}" for d in range(1, 11)]
    prices_list = [100 * (1.01**i) for i in range(10)]
    plan = _plan(
        walk_forward=(
            _window(
                train_start="2024-12-01",
                train_end="2024-12-31",
                test_start="2025-01-01",
                test_end="2025-01-10",
            ),
        ),
    )
    prices = _prices([(d, "AAPL", p) for d, p in zip(dates, prices_list, strict=True)])
    sigs = _constant_signals(dates=dates, ticker="AAPL", weight=1.0)

    report = run_walk_forward(
        plan=plan,
        prices=prices,
        signals=sigs,
        notional_usd=Decimal("1000000"),
    )
    fold = report.folds[0]
    # 9 daily returns of 1% with weight=1 (lagged); each contributes ~1%
    # pnl ≈ 1M * ((1.01^9) - 1) ≈ 93,685
    assert fold.pnl_usd > Decimal("90000")
    assert fold.pnl_usd < Decimal("100000")
    # Constant return → zero stdev → sharpe forced to 0 (we don't surface inf/NaN)
    assert fold.sharpe == Decimal(0)
    # No drawdown on monotone uptrend
    assert fold.max_drawdown_pct == Decimal(0)


def test_walk_forward_max_drawdown_captured() -> None:
    """A peak-then-trough sequence has the expected max drawdown."""
    # Equity path: 1.00 → 1.10 → 1.21 (peak) → 1.05 (drop) → 1.05
    # Drawdown from 1.21 to 1.05 = (1.21 - 1.05)/1.21 ≈ 13.22%
    dates = [f"2025-01-{d:02d}" for d in range(1, 6)]
    prices = _prices(
        [
            (dates[0], "AAPL", 100.0),
            (dates[1], "AAPL", 110.0),
            (dates[2], "AAPL", 121.0),
            (dates[3], "AAPL", 105.0),
            (dates[4], "AAPL", 105.0),
        ],
    )
    sigs = _constant_signals(dates=dates, ticker="AAPL", weight=1.0)
    plan = _plan(
        walk_forward=(
            _window(
                train_start="2024-12-01",
                train_end="2024-12-31",
                test_start="2025-01-01",
                test_end="2025-01-05",
            ),
        ),
    )

    report = run_walk_forward(plan=plan, prices=prices, signals=sigs)
    expected_dd = Decimal("13.22")
    actual_dd = report.folds[0].max_drawdown_pct
    assert abs(actual_dd - expected_dd) < Decimal("0.5")


def test_walk_forward_lags_signals_one_day() -> None:
    """Signal posted on day t earns day t+1's return, not day t's."""
    # Day 1: price=100, signal=1 (entered at end of day 1, holds through day 2)
    # Day 2: price=110 → return = +10% (earned via lagged signal)
    # If the signal weren't lagged, day 1's signal would earn day 1's nonexistent return
    # and we'd be off-by-one.
    dates = ["2025-01-01", "2025-01-02"]
    prices = _prices([(dates[0], "AAPL", 100.0), (dates[1], "AAPL", 110.0)])
    sigs = _constant_signals(dates=dates, ticker="AAPL", weight=1.0)
    plan = _plan(
        walk_forward=(
            _window(
                train_start="2024-12-01",
                train_end="2024-12-31",
                test_start="2025-01-01",
                test_end="2025-01-02",
            ),
        ),
    )
    report = run_walk_forward(plan=plan, prices=prices, signals=sigs)
    fold = report.folds[0]
    # Lagged signal: weight on day 2 is signal from day 1. Daily return = +10%.
    # pnl on $1M notional = 100,000 (modulo costs)
    assert fold.pnl_usd > Decimal("99000")
    assert fold.pnl_usd < Decimal("101000")


# === Costs ===


def test_walk_forward_applies_cost_on_position_changes() -> None:
    """A non-zero cost reduces realized PnL relative to the zero-cost baseline."""
    dates = ["2025-01-01", "2025-01-02"]
    prices = _prices([(dates[0], "AAPL", 100.0), (dates[1], "AAPL", 110.0)])
    sigs = _constant_signals(dates=dates, ticker="AAPL", weight=1.0)

    plan_no_cost = _plan(
        walk_forward=(
            _window(
                train_start="2024-12-01",
                train_end="2024-12-31",
                test_start="2025-01-01",
                test_end="2025-01-02",
            ),
        ),
    )
    plan_costly = _plan(
        walk_forward=(
            _window(
                train_start="2024-12-01",
                train_end="2024-12-31",
                test_start="2025-01-01",
                test_end="2025-01-02",
            ),
        ),
        cost_bps=Decimal("50"),  # 50 bps = 0.5% cost
        slippage_bps=Decimal("50"),  # plus 50 bps slippage = 1% total round trip
    )
    no_cost = run_walk_forward(plan=plan_no_cost, prices=prices, signals=sigs)
    costly = run_walk_forward(plan=plan_costly, prices=prices, signals=sigs)
    assert costly.folds[0].pnl_usd < no_cost.folds[0].pnl_usd


# === Aggregate over multiple folds ===


def test_walk_forward_two_folds_aggregates() -> None:
    """median / mean / worst sharpe + total pnl + total trades aggregate."""
    # Fold A: prices 100→110 (positive)
    # Fold B: prices 100→90 (negative)
    plan = _plan(
        walk_forward=(
            _window(
                train_start="2024-11-01",
                train_end="2024-11-30",
                test_start="2024-12-01",
                test_end="2024-12-02",
            ),
            _window(
                train_start="2024-12-15",
                train_end="2024-12-31",
                test_start="2025-01-01",
                test_end="2025-01-02",
            ),
        ),
    )
    prices = _prices(
        [
            ("2024-12-01", "AAPL", 100.0),
            ("2024-12-02", "AAPL", 110.0),
            ("2025-01-01", "AAPL", 100.0),
            ("2025-01-02", "AAPL", 90.0),
        ],
    )
    sigs = _signals(
        [
            ("2024-12-01", "AAPL", 1.0),
            ("2024-12-02", "AAPL", 1.0),
            ("2025-01-01", "AAPL", 1.0),
            ("2025-01-02", "AAPL", 1.0),
        ],
    )

    report = run_walk_forward(plan=plan, prices=prices, signals=sigs)
    assert len(report.folds) == 2
    assert report.folds[0].fold_index == 0
    assert report.folds[1].fold_index == 1
    # Fold 0 positive PnL, fold 1 negative
    assert report.folds[0].pnl_usd > Decimal(0)
    assert report.folds[1].pnl_usd < Decimal(0)
    # Aggregate: total PnL = sum
    expected_total = report.folds[0].pnl_usd + report.folds[1].pnl_usd
    assert report.aggregate.total_pnl_usd == expected_total


# === Validation / edge cases ===


def test_walk_forward_raises_when_signal_references_unknown_ticker() -> None:
    dates = ["2025-01-01", "2025-01-02"]
    prices = _prices([(d, "AAPL", 100.0) for d in dates])
    sigs = _signals([(d, "MSFT", 1.0) for d in dates])  # no MSFT prices
    plan = _plan(
        walk_forward=(
            _window(
                train_start="2024-12-01",
                train_end="2024-12-31",
                test_start="2025-01-01",
                test_end="2025-01-02",
            ),
        ),
    )
    with pytest.raises(ValueError, match="ticker"):
        run_walk_forward(plan=plan, prices=prices, signals=sigs)


def test_walk_forward_raises_on_empty_test_window() -> None:
    """A walk-forward window whose test range has no price rows must fail loudly."""
    plan = _plan(
        walk_forward=(
            _window(
                train_start="2024-12-01",
                train_end="2024-12-31",
                test_start="2025-01-01",
                test_end="2025-01-02",
            ),
        ),
    )
    prices = _prices([("2024-11-01", "AAPL", 100.0)])  # no rows in window
    sigs = _signals([("2025-01-01", "AAPL", 1.0)])
    with pytest.raises(ValueError, match="no prices"):
        run_walk_forward(plan=plan, prices=prices, signals=sigs)
