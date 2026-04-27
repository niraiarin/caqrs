"""Property-based round-trip and validation tests for artifact schemas."""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from caqrs.schemas.backtest_report import (
    AggregateMetrics,
    BacktestReport,
    FoldMetrics,
)
from caqrs.schemas.common import (
    RunMetadata,
    StrictBaseModel,
    new_run_id,
    utc_now,
)
from caqrs.schemas.decision import (
    DecisionAction,
    Side,
    StrategyDecision,
    TargetPosition,
)
from caqrs.schemas.hypothesis_card import (
    AcceptanceCriterion,
    Direction,
    HypothesisCard,
    HypothesisStatus,
)
from caqrs.schemas.research_plan import (
    DataFrequency,
    ResearchPlan,
    WalkForwardWindow,
)

# === Strategies ===


@st.composite
def run_metadata(draw: st.DrawFn) -> RunMetadata:
    return RunMetadata(
        run_id=new_run_id(),
        parent_id=draw(st.one_of(st.none(), st.just(new_run_id()))),
        agent_name=draw(
            st.text(
                min_size=1, max_size=40, alphabet=st.characters(min_codepoint=33, max_codepoint=126)
            )
        ),
        model_id=draw(st.sampled_from(["anthropic/opus", "openai/gpt-4o", "deepseek/v3", "test"])),
        created_at=utc_now(),
        llm_cost_usd=draw(
            st.decimals(
                min_value=Decimal(0),
                max_value=Decimal("100"),
                places=4,
                allow_nan=False,
                allow_infinity=False,
            )
        ),
        latency_ms=draw(st.integers(min_value=0, max_value=300_000)),
        token_in=draw(st.integers(min_value=0, max_value=200_000)),
        token_out=draw(st.integers(min_value=0, max_value=200_000)),
    )


def assert_roundtrip(model: StrictBaseModel) -> None:
    js = model.model_dump_json()
    restored = type(model).model_validate_json(js)
    assert restored == model


def make_meta(now: datetime, agent: str = "test-agent") -> RunMetadata:
    return RunMetadata(
        run_id=new_run_id(),
        parent_id=None,
        agent_name=agent,
        model_id="test",
        created_at=now,
    )


# === RunMetadata ===


@settings(max_examples=50)
@given(meta=run_metadata())
def test_run_metadata_roundtrip(meta: RunMetadata) -> None:
    assert_roundtrip(meta)


def test_run_metadata_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError):
        RunMetadata(
            run_id=new_run_id(),
            parent_id=None,
            agent_name="x",
            model_id="m",
            created_at=datetime(2026, 1, 1),  # naive — must fail
        )


def test_run_metadata_rejects_bad_run_id() -> None:
    with pytest.raises(ValidationError):
        RunMetadata(
            run_id="not-hex",
            parent_id=None,
            agent_name="x",
            model_id="m",
            created_at=utc_now(),
        )


# === HypothesisCard ===


@settings(max_examples=20)
@given(meta=run_metadata(), horizon=st.integers(min_value=1, max_value=365))
def test_hypothesis_card_roundtrip(meta: RunMetadata, horizon: int) -> None:
    now = utc_now()
    card = HypothesisCard(
        metadata=meta,
        status=HypothesisStatus.DRAFT,
        claim="Momentum on US large-caps outperforms equal-weight in low-vol regimes.",
        rationale="Heuristic from Jegadeesh & Titman; Skeptic to challenge regime detection.",
        universe=("AAPL", "MSFT", "GOOGL"),
        direction=Direction.LONG,
        horizon_days=horizon,
        variables=("realized_vol_30d", "12_1_momentum"),
        acceptance=(
            AcceptanceCriterion(
                metric_path="aggregate.median_sharpe",
                op=">=",
                threshold=Decimal("0.5"),
            ),
        ),
        max_drawdown_pct=Decimal("20"),
        expected_window_start=now,
        expected_window_end=now + timedelta(days=horizon),
    )
    assert_roundtrip(card)


def test_hypothesis_card_rejects_inverted_window() -> None:
    now = utc_now()
    meta = make_meta(now)
    with pytest.raises(ValidationError):
        HypothesisCard(
            metadata=meta,
            status=HypothesisStatus.DRAFT,
            claim="x" * 20,
            rationale="r",
            universe=("AAPL",),
            direction=Direction.LONG,
            horizon_days=10,
            variables=("v",),
            acceptance=(AcceptanceCriterion(metric_path="m", op=">=", threshold=Decimal("0.5")),),
            max_drawdown_pct=Decimal("20"),
            expected_window_start=now + timedelta(days=10),
            expected_window_end=now,
        )


def test_hypothesis_card_rejects_duplicate_universe() -> None:
    now = utc_now()
    meta = make_meta(now)
    with pytest.raises(ValidationError):
        HypothesisCard(
            metadata=meta,
            status=HypothesisStatus.DRAFT,
            claim="x" * 20,
            rationale="r",
            universe=("AAPL", "AAPL"),
            direction=Direction.LONG,
            horizon_days=10,
            variables=("v",),
            acceptance=(AcceptanceCriterion(metric_path="m", op=">=", threshold=Decimal(0)),),
            max_drawdown_pct=Decimal("20"),
            expected_window_start=now,
            expected_window_end=now + timedelta(days=10),
        )


# === ResearchPlan ===


def test_walk_forward_rejects_train_test_overlap() -> None:
    now = utc_now()
    with pytest.raises(ValidationError):
        WalkForwardWindow(
            train_start=now,
            train_end=now + timedelta(days=10),
            test_start=now + timedelta(days=5),  # test starts inside train
            test_end=now + timedelta(days=20),
        )


def test_research_plan_roundtrip_minimal() -> None:
    now = utc_now()
    meta = make_meta(now, "research")
    plan = ResearchPlan(
        metadata=meta,
        hypothesis_run_id=new_run_id(),
        universe=("AAPL", "MSFT"),
        frequency=DataFrequency.DAILY,
        walk_forward=(
            WalkForwardWindow(
                train_start=now,
                train_end=now + timedelta(days=100),
                test_start=now + timedelta(days=100),
                test_end=now + timedelta(days=130),
            ),
            WalkForwardWindow(
                train_start=now + timedelta(days=30),
                train_end=now + timedelta(days=130),
                test_start=now + timedelta(days=130),
                test_end=now + timedelta(days=160),
            ),
        ),
        cost_model_bps=Decimal("3"),
        slippage_bps=Decimal("1"),
        seed=42,
    )
    assert_roundtrip(plan)


def test_research_plan_rejects_overlapping_test_windows() -> None:
    now = utc_now()
    meta = make_meta(now, "research")
    with pytest.raises(ValidationError):
        ResearchPlan(
            metadata=meta,
            hypothesis_run_id=new_run_id(),
            universe=("AAPL",),
            frequency=DataFrequency.DAILY,
            walk_forward=(
                WalkForwardWindow(
                    train_start=now,
                    train_end=now + timedelta(days=50),
                    test_start=now + timedelta(days=50),
                    test_end=now + timedelta(days=80),
                ),
                WalkForwardWindow(
                    train_start=now + timedelta(days=10),
                    train_end=now + timedelta(days=60),
                    test_start=now + timedelta(days=60),  # < previous test_end=80 → overlap
                    test_end=now + timedelta(days=90),
                ),
            ),
            cost_model_bps=Decimal("3"),
            slippage_bps=Decimal("1"),
            seed=1,
        )


# === BacktestReport ===


def test_backtest_report_roundtrip_minimal() -> None:
    now = utc_now()
    meta = make_meta(now, "research")
    folds = (
        FoldMetrics(
            fold_index=0,
            test_start=now,
            test_end=now + timedelta(days=30),
            sharpe=Decimal("0.6"),
            max_drawdown_pct=Decimal("8.5"),
            turnover=Decimal("1.2"),
            n_trades=12,
            pnl_usd=Decimal("1234.56"),
        ),
        FoldMetrics(
            fold_index=1,
            test_start=now + timedelta(days=30),
            test_end=now + timedelta(days=60),
            sharpe=Decimal("0.4"),
            max_drawdown_pct=Decimal("12.0"),
            turnover=Decimal("1.5"),
            n_trades=14,
            pnl_usd=Decimal("-200.00"),
        ),
    )
    aggregate = AggregateMetrics(
        median_sharpe=Decimal("0.5"),
        mean_sharpe=Decimal("0.5"),
        worst_fold_sharpe=Decimal("0.4"),
        median_max_drawdown_pct=Decimal("10.25"),
        total_pnl_usd=Decimal("1034.56"),
        total_trades=26,
    )
    report = BacktestReport(
        metadata=meta,
        plan_run_id=new_run_id(),
        folds=folds,
        aggregate=aggregate,
    )
    assert_roundtrip(report)


def test_backtest_report_rejects_non_contiguous_fold_index() -> None:
    now = utc_now()
    meta = make_meta(now, "research")
    aggregate = AggregateMetrics(
        median_sharpe=Decimal(0),
        mean_sharpe=Decimal(0),
        worst_fold_sharpe=Decimal(0),
        median_max_drawdown_pct=Decimal(0),
        total_pnl_usd=Decimal(0),
        total_trades=0,
    )
    with pytest.raises(ValidationError):
        BacktestReport(
            metadata=meta,
            plan_run_id=new_run_id(),
            folds=(
                FoldMetrics(
                    fold_index=2,  # should be 0
                    test_start=now,
                    test_end=now + timedelta(days=30),
                    sharpe=Decimal(0),
                    max_drawdown_pct=Decimal(0),
                    turnover=Decimal(0),
                    n_trades=0,
                    pnl_usd=Decimal(0),
                ),
            ),
            aggregate=aggregate,
        )


# === StrategyDecision ===


def test_strategy_decision_adopt_roundtrip() -> None:
    now = utc_now()
    meta = make_meta(now, "decider")
    decision = StrategyDecision(
        metadata=meta,
        backtest_run_id=new_run_id(),
        action=DecisionAction.ADOPT,
        targets=(
            TargetPosition(ticker="AAPL", side=Side.BUY, weight=Decimal("0.3")),
            TargetPosition(ticker="MSFT", side=Side.BUY, weight=Decimal("0.4")),
        ),
        rationale="Backtest cleared all acceptance criteria.",
        notional_cap_usd=Decimal("100000"),
        max_position_weight=Decimal("0.5"),
        daily_loss_limit_usd=Decimal("5000"),
    )
    assert_roundtrip(decision)


def test_strategy_decision_reject_must_have_no_targets() -> None:
    now = utc_now()
    meta = make_meta(now, "decider")
    with pytest.raises(ValidationError):
        StrategyDecision(
            metadata=meta,
            backtest_run_id=new_run_id(),
            action=DecisionAction.REJECT,
            targets=(TargetPosition(ticker="AAPL", side=Side.BUY, weight=Decimal("0.1")),),
            rationale="r",
            notional_cap_usd=Decimal(0),
            max_position_weight=Decimal("0.5"),
            daily_loss_limit_usd=Decimal(0),
        )


def test_strategy_decision_adopt_requires_targets() -> None:
    now = utc_now()
    meta = make_meta(now, "decider")
    with pytest.raises(ValidationError):
        StrategyDecision(
            metadata=meta,
            backtest_run_id=new_run_id(),
            action=DecisionAction.ADOPT,
            targets=(),
            rationale="r",
            notional_cap_usd=Decimal(100),
            max_position_weight=Decimal("0.5"),
            daily_loss_limit_usd=Decimal(0),
        )


def test_strategy_decision_weight_exceeds_max() -> None:
    now = utc_now()
    meta = make_meta(now, "decider")
    with pytest.raises(ValidationError):
        StrategyDecision(
            metadata=meta,
            backtest_run_id=new_run_id(),
            action=DecisionAction.ADOPT,
            targets=(TargetPosition(ticker="AAPL", side=Side.BUY, weight=Decimal("0.6")),),
            rationale="r",
            notional_cap_usd=Decimal(100),
            max_position_weight=Decimal("0.5"),
            daily_loss_limit_usd=Decimal(0),
        )


def test_strategy_decision_sum_weights_exceeds_one() -> None:
    now = utc_now()
    meta = make_meta(now, "decider")
    with pytest.raises(ValidationError):
        StrategyDecision(
            metadata=meta,
            backtest_run_id=new_run_id(),
            action=DecisionAction.ADOPT,
            targets=(
                TargetPosition(ticker="AAPL", side=Side.BUY, weight=Decimal("0.5")),
                TargetPosition(ticker="MSFT", side=Side.BUY, weight=Decimal("0.4")),
                TargetPosition(ticker="GOOGL", side=Side.BUY, weight=Decimal("0.4")),
            ),
            rationale="r",
            notional_cap_usd=Decimal(100),
            max_position_weight=Decimal("0.6"),
            daily_loss_limit_usd=Decimal(0),
        )


def test_strategy_decision_rejects_duplicate_tickers() -> None:
    now = utc_now()
    meta = make_meta(now, "decider")
    with pytest.raises(ValidationError):
        StrategyDecision(
            metadata=meta,
            backtest_run_id=new_run_id(),
            action=DecisionAction.ADOPT,
            targets=(
                TargetPosition(ticker="AAPL", side=Side.BUY, weight=Decimal("0.2")),
                TargetPosition(ticker="AAPL", side=Side.SELL, weight=Decimal("0.3")),
            ),
            rationale="r",
            notional_cap_usd=Decimal(100),
            max_position_weight=Decimal("0.5"),
            daily_loss_limit_usd=Decimal(0),
        )
