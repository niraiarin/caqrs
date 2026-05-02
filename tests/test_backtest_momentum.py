"""Tests for the momentum signal template + executor factory."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import polars as pl
import pytest
import respx

from caqrs.backtest import (
    BacktestExecutor,
    make_jquants_momentum_executor,
    momentum_signals,
    run_walk_forward,
)
from caqrs.data.jquants import JQuantsClient
from caqrs.schemas.backtest_report import BacktestReport
from caqrs.schemas.common import RunMetadata, new_run_id
from caqrs.schemas.research_plan import (
    DataFrequency,
    ResearchPlan,
    WalkForwardWindow,
)

_BASE = "https://api.jquants.com/v2"
_KEY = "jq-test-key"


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


def _plan_one_fold(
    *,
    universe: tuple[str, ...] = ("13010", "72030"),
    test_start: date = date(2025, 6, 1),
    test_end: date = date(2025, 6, 30),
) -> ResearchPlan:
    return ResearchPlan(
        metadata=_meta(),
        hypothesis_run_id=new_run_id(),
        universe=universe,
        frequency=DataFrequency.DAILY,
        walk_forward=(
            WalkForwardWindow(
                train_start=datetime(2025, 1, 1, tzinfo=UTC),
                train_end=datetime(2025, 5, 31, tzinfo=UTC),
                test_start=datetime.combine(test_start, datetime.min.time(), tzinfo=UTC),
                test_end=datetime.combine(test_end, datetime.min.time(), tzinfo=UTC),
            ),
        ),
        cost_model_bps=Decimal(0),
        slippage_bps=Decimal(0),
        seed=1,
    )


def _prices_long(rows: list[tuple[str, str, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "date": [datetime.fromisoformat(d).replace(tzinfo=UTC) for d, _, _ in rows],
            "ticker": [t for _, t, _ in rows],
            "close": [c for _, _, c in rows],
        },
    )


# === momentum_signals: ranking ===


def test_momentum_top_k_equals_universe_size_matches_buy_and_hold() -> None:
    """When top_k = N every ticker gets weight 1/N every day with enough
    history. Equivalent to buy-and-hold rebalanced daily."""
    dates = [f"2025-06-{d:02d}" for d in range(1, 11)]
    prices = _prices_long(
        [(d, "AAPL", float(100 + i)) for i, d in enumerate(dates)]
        + [(d, "MSFT", float(200 + i * 0.5)) for i, d in enumerate(dates)],
    )
    plan = _plan_one_fold(universe=("AAPL", "MSFT"))
    sigs = momentum_signals(
        plan=plan,
        prices=prices,
        lookback_days=2,
        top_k=2,
    )
    # Days where lookback history is available: 8 days x 2 tickers = 16 rows
    rows_with_signal = sigs.filter(pl.col("weight") > 0).height
    expected_rows = 8 * 2
    assert rows_with_signal == expected_rows
    nonzero = (
        sigs.filter(pl.col("weight") > 0).select(pl.col("weight").unique()).to_series().to_list()
    )
    assert nonzero == [0.5]


@pytest.mark.traces("BACKTEST-A3")
def test_momentum_top_1_picks_highest_lookback_return() -> None:
    """With two tickers and top_k=1, the ticker with the higher
    lookback-day return earns the full weight; the other gets zero."""
    prices = _prices_long(
        [
            ("2025-06-01", "AAPL", 100.0),
            ("2025-06-02", "AAPL", 105.0),
            ("2025-06-03", "AAPL", 110.0),
            ("2025-06-04", "AAPL", 115.0),
            ("2025-06-05", "AAPL", 120.0),
            ("2025-06-01", "MSFT", 100.0),
            ("2025-06-02", "MSFT", 100.5),
            ("2025-06-03", "MSFT", 101.0),
            ("2025-06-04", "MSFT", 101.5),
            ("2025-06-05", "MSFT", 102.0),
        ],
    )
    plan = _plan_one_fold(universe=("AAPL", "MSFT"))
    sigs = momentum_signals(
        plan=plan,
        prices=prices,
        lookback_days=2,
        top_k=1,
    )
    aapl_signals = sigs.filter(
        (pl.col("ticker") == "AAPL") & (pl.col("weight") > 0),
    )
    msft_signals = sigs.filter(
        (pl.col("ticker") == "MSFT") & (pl.col("weight") > 0),
    )
    assert aapl_signals.height == 3
    assert msft_signals.height == 0
    aapl_weights = aapl_signals["weight"].unique().to_list()
    assert aapl_weights == [1.0]


def test_momentum_zero_weights_when_insufficient_history() -> None:
    """The first lookback_days dates can not compute a return; weights are 0."""
    dates = [f"2025-06-{d:02d}" for d in range(1, 5)]
    prices = _prices_long(
        [(d, "AAPL", float(100 + i)) for i, d in enumerate(dates)]
        + [(d, "MSFT", float(200 + i)) for i, d in enumerate(dates)],
    )
    plan = _plan_one_fold(universe=("AAPL", "MSFT"))
    sigs = momentum_signals(
        plan=plan,
        prices=prices,
        lookback_days=2,
        top_k=2,
    )
    day1 = sigs.filter(pl.col("date") == datetime(2025, 6, 1, tzinfo=UTC))
    assert day1["weight"].unique().to_list() == [0.0]
    day2 = sigs.filter(pl.col("date") == datetime(2025, 6, 2, tzinfo=UTC))
    assert day2["weight"].unique().to_list() == [0.0]
    day3 = sigs.filter(pl.col("date") == datetime(2025, 6, 3, tzinfo=UTC))
    assert (day3.filter(pl.col("weight") > 0)).height >= 1


def test_momentum_top_k_default_is_full_universe() -> None:
    """top_k=None defaults to len(plan.universe)."""
    dates = [f"2025-06-{d:02d}" for d in range(1, 6)]
    prices = _prices_long(
        [(d, "AAPL", float(100 + i)) for i, d in enumerate(dates)]
        + [(d, "MSFT", float(200 + i)) for i, d in enumerate(dates)],
    )
    plan = _plan_one_fold(universe=("AAPL", "MSFT"))
    sigs = momentum_signals(
        plan=plan,
        prices=prices,
        lookback_days=2,
        top_k=None,
    )
    nonzero = (
        sigs.filter(pl.col("weight") > 0).select(pl.col("weight").unique()).to_series().to_list()
    )
    assert nonzero == [0.5]


def test_momentum_rejects_top_k_larger_than_universe() -> None:
    plan = _plan_one_fold(universe=("AAPL", "MSFT"))
    prices = _prices_long(
        [
            ("2025-06-01", "AAPL", 100.0),
            ("2025-06-01", "MSFT", 200.0),
        ],
    )
    with pytest.raises(ValueError, match="top_k"):
        momentum_signals(
            plan=plan,
            prices=prices,
            lookback_days=1,
            top_k=5,
        )


def test_momentum_rejects_nonpositive_lookback() -> None:
    plan = _plan_one_fold(universe=("AAPL",))
    prices = _prices_long([("2025-06-01", "AAPL", 100.0)])
    with pytest.raises(ValueError, match="lookback_days"):
        momentum_signals(plan=plan, prices=prices, lookback_days=0)


def test_momentum_signals_compose_with_run_walk_forward() -> None:
    """End-to-end: build momentum signals, run them through the engine,
    confirm a sensible BacktestReport comes out."""
    dates = [f"2025-06-{d:02d}" for d in range(1, 11)]
    aapl_prices = [float(100 + i) * 2 for i in range(10)]
    msft_prices = [float(200 + i * 0.5) for i in range(10)]
    prices = _prices_long(
        [(d, "AAPL", p) for d, p in zip(dates, aapl_prices, strict=True)]
        + [(d, "MSFT", p) for d, p in zip(dates, msft_prices, strict=True)],
    )
    plan = _plan_one_fold(
        universe=("AAPL", "MSFT"),
        test_start=date(2025, 6, 1),
        test_end=date(2025, 6, 10),
    )
    sigs = momentum_signals(plan=plan, prices=prices, lookback_days=2, top_k=1)
    report = run_walk_forward(plan=plan, prices=prices, signals=sigs)
    assert len(report.folds) == 1
    fold = report.folds[0]
    assert fold.pnl_usd > Decimal(0)


# === Factory: make_jquants_momentum_executor ===


def _bars_payload(records: list[dict[str, object]]) -> dict[str, object]:
    return {"data": records}


@pytest.mark.asyncio
@respx.mock
async def test_make_jquants_momentum_executor_returns_protocol() -> None:
    async with JQuantsClient(api_key=_KEY) as client:
        executor: BacktestExecutor = make_jquants_momentum_executor(
            client=client,
            lookback_days=5,
            top_k=1,
        )
        assert callable(executor)


@pytest.mark.asyncio
@respx.mock
async def test_make_jquants_momentum_executor_pre_fetches_with_buffer() -> None:
    """Factory must request prices starting earlier than the first
    test_start so the day-1 momentum rank can be computed."""
    captured_ranges: list[tuple[str, str]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured_ranges.append(
            (request.url.params["from"], request.url.params["to"]),
        )
        return httpx.Response(200, json=_bars_payload([]))

    respx.get(f"{_BASE}/equities/bars/daily").mock(side_effect=_handler)

    plan = _plan_one_fold(
        universe=("13010",),
        test_start=date(2025, 6, 1),
        test_end=date(2025, 6, 30),
    )

    async with JQuantsClient(api_key=_KEY) as client:
        executor = make_jquants_momentum_executor(
            client=client,
            lookback_days=21,
            top_k=1,
        )
        with pytest.raises(ValueError, match="no prices"):
            await executor(plan)

    assert len(captured_ranges) == 1
    from_str = captured_ranges[0][0]
    from_d = datetime.strptime(from_str, "%Y%m%d").date()
    assert from_d < date(2025, 6, 1)
    assert (date(2025, 6, 1) - from_d) >= timedelta(days=21)


@pytest.mark.asyncio
@respx.mock
async def test_make_jquants_momentum_executor_runs_full_pipeline() -> None:
    """End-to-end: factory -> fetch prices -> build momentum signals ->
    run_walk_forward -> BacktestReport. Mocks J-Quants with a clear
    momentum signal."""
    days_iso = [f"2025-05-{d:02d}" for d in range(1, 32)] + [
        f"2025-06-{d:02d}" for d in range(1, 11)
    ]
    aapl_closes = [100.0 + i * 1.0 for i in range(len(days_iso))]
    msft_closes = [200.0 + i * 0.1 for i in range(len(days_iso))]

    def _handler(request: httpx.Request) -> httpx.Response:
        code = request.url.params["code"]
        closes = aapl_closes if code == "AAPL" else msft_closes
        return httpx.Response(
            200,
            json=_bars_payload(
                [
                    {"Date": d, "Code": code, "C": str(c), "AdjC": str(c)}
                    for d, c in zip(days_iso, closes, strict=True)
                ],
            ),
        )

    respx.get(f"{_BASE}/equities/bars/daily").mock(side_effect=_handler)

    async with JQuantsClient(api_key=_KEY) as client:
        executor = make_jquants_momentum_executor(
            client=client,
            lookback_days=10,
            top_k=1,
        )
        plan = _plan_one_fold(
            universe=("AAPL", "MSFT"),
            test_start=date(2025, 6, 1),
            test_end=date(2025, 6, 10),
        )
        report = await executor(plan)

    assert isinstance(report, BacktestReport)
    fold = report.folds[0]
    assert fold.pnl_usd > Decimal(0)
