"""Tests for the strategy template registry / discriminated union."""

from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
import respx
from pydantic import TypeAdapter, ValidationError

from caqrs.backtest import (
    BacktestExecutor,
    BuyAndHoldSpec,
    MeanReversionSpec,
    MomentumSpec,
    StrategySpec,
    make_jquants_executor,
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
    test_end: date = date(2025, 6, 10),
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


def _bars_payload(records: list[dict[str, object]]) -> dict[str, object]:
    return {"data": records}


# === Spec construction ===


def test_buy_and_hold_spec_has_correct_discriminator() -> None:
    spec = BuyAndHoldSpec()
    assert spec.template == "buy_and_hold"


def test_momentum_spec_construction() -> None:
    spec = MomentumSpec(lookback_days=21, top_k=3)
    assert spec.template == "momentum"
    assert spec.lookback_days == 21
    assert spec.top_k == 3


def test_momentum_spec_default_top_k_is_none() -> None:
    spec = MomentumSpec(lookback_days=21)
    assert spec.top_k is None


def test_momentum_spec_rejects_nonpositive_lookback() -> None:
    with pytest.raises(ValidationError):
        MomentumSpec(lookback_days=0)


def test_mean_reversion_spec_construction() -> None:
    spec = MeanReversionSpec(lookback_days=10, bottom_k=1)
    assert spec.template == "mean_reversion"
    assert spec.lookback_days == 10
    assert spec.bottom_k == 1


def test_strategy_spec_is_frozen_extra_forbid() -> None:
    """Specs inherit StrictBaseModel-like guarantees so unknown fields are
    a hard error and nothing can be mutated post-construction."""
    spec = MomentumSpec(lookback_days=21, top_k=1)
    with pytest.raises(ValidationError, match="frozen"):
        spec.lookback_days = 30  # type: ignore[misc]


def test_momentum_spec_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        MomentumSpec(lookback_days=21, top_k=1, bottom_k=1)  # type: ignore[call-arg]


# === Discriminated union parsing ===


def test_strategy_spec_union_parses_buy_and_hold() -> None:
    parsed: StrategySpec = TypeAdapter(StrategySpec).validate_python(
        {"template": "buy_and_hold"},
    )
    assert isinstance(parsed, BuyAndHoldSpec)


def test_strategy_spec_union_parses_momentum() -> None:
    parsed: StrategySpec = TypeAdapter(StrategySpec).validate_python(
        {"template": "momentum", "lookback_days": 21, "top_k": 1},
    )
    assert isinstance(parsed, MomentumSpec)
    assert parsed.lookback_days == 21


def test_strategy_spec_union_parses_mean_reversion() -> None:
    parsed: StrategySpec = TypeAdapter(StrategySpec).validate_python(
        {"template": "mean_reversion", "lookback_days": 10, "bottom_k": 2},
    )
    assert isinstance(parsed, MeanReversionSpec)
    assert parsed.bottom_k == 2


def test_strategy_spec_union_rejects_unknown_template() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(StrategySpec).validate_python({"template": "buy_high_sell_low"})


def test_strategy_spec_union_round_trips_through_json() -> None:
    """Specs serialise + parse back to the same instance — important for
    when ResearchPlan eventually carries a StrategySpec field."""
    original = MomentumSpec(lookback_days=21, top_k=3)
    payload = original.model_dump_json()
    restored: StrategySpec = TypeAdapter(StrategySpec).validate_json(payload)
    assert isinstance(restored, MomentumSpec)
    assert restored == original


# === Dispatcher ===


@pytest.mark.asyncio
async def test_make_jquants_executor_dispatches_buy_and_hold() -> None:
    async with JQuantsClient(api_key=_KEY) as client:
        executor: BacktestExecutor = make_jquants_executor(
            spec=BuyAndHoldSpec(),
            client=client,
        )
        assert callable(executor)


@pytest.mark.asyncio
async def test_make_jquants_executor_dispatches_momentum() -> None:
    async with JQuantsClient(api_key=_KEY) as client:
        executor = make_jquants_executor(
            spec=MomentumSpec(lookback_days=21, top_k=1),
            client=client,
        )
        assert callable(executor)


@pytest.mark.asyncio
async def test_make_jquants_executor_dispatches_mean_reversion() -> None:
    async with JQuantsClient(api_key=_KEY) as client:
        executor = make_jquants_executor(
            spec=MeanReversionSpec(lookback_days=10, bottom_k=1),
            client=client,
        )
        assert callable(executor)


@pytest.mark.asyncio
@respx.mock
async def test_dispatched_executor_runs_full_pipeline() -> None:
    """Full circuit: build a MomentumSpec, dispatch via make_jquants_executor,
    feed it a ResearchPlan, get a BacktestReport. Mocks J-Quants to keep
    the test offline."""
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
        executor = make_jquants_executor(
            spec=MomentumSpec(lookback_days=10, top_k=1),
            client=client,
        )
        plan = _plan_one_fold(
            universe=("AAPL", "MSFT"),
            test_start=date(2025, 6, 1),
            test_end=date(2025, 6, 10),
        )
        report = await executor(plan)

    assert isinstance(report, BacktestReport)
    fold = report.folds[0]
    # AAPL trends up faster → momentum picks AAPL → positive PnL
    assert fold.pnl_usd > Decimal(0)


@pytest.mark.asyncio
async def test_make_jquants_executor_carries_notional_through() -> None:
    """notional_usd kwarg propagates to the underlying factory."""
    async with JQuantsClient(api_key=_KEY) as client:
        # Construction must not fail with a non-default notional.
        executor = make_jquants_executor(
            spec=BuyAndHoldSpec(),
            client=client,
            notional_usd=Decimal("250000"),
        )
        assert callable(executor)
