"""Backtest Report: the outcome of executing a ResearchPlan.

Produced by the Research Agent. Consumed by the Auditor and Strategy
Committee. Folds must be contiguous (fold_index 0..N-1) so out-of-sample
coverage is verifiable.
"""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Self

from pydantic import Field, model_validator

from caqrs.schemas.common import RunId, RunMetadata, StrictBaseModel


class FoldMetrics(StrictBaseModel):
    fold_index: Annotated[int, Field(ge=0)]
    test_start: datetime
    test_end: datetime
    sharpe: Decimal
    max_drawdown_pct: Annotated[Decimal, Field(ge=0, le=Decimal(100))]
    turnover: Annotated[Decimal, Field(ge=0)]
    n_trades: Annotated[int, Field(ge=0)]
    pnl_usd: Decimal


class AggregateMetrics(StrictBaseModel):
    median_sharpe: Decimal
    mean_sharpe: Decimal
    worst_fold_sharpe: Decimal
    median_max_drawdown_pct: Annotated[Decimal, Field(ge=0, le=Decimal(100))]
    total_pnl_usd: Decimal
    total_trades: Annotated[int, Field(ge=0)]


class BacktestReport(StrictBaseModel):
    schema_version: int = Field(default=1, ge=1, le=1)
    metadata: RunMetadata
    plan_run_id: RunId

    folds: tuple[FoldMetrics, ...] = Field(min_length=1, max_length=200)
    aggregate: AggregateMetrics

    @model_validator(mode="after")
    def _folds_contiguous(self) -> Self:
        for i, fold in enumerate(self.folds):
            if fold.fold_index != i:
                raise ValueError(
                    f"folds must have contiguous fold_index starting at 0; "
                    f"position {i} has fold_index={fold.fold_index}.",
                )
            if fold.test_end <= fold.test_start:
                raise ValueError(f"fold {i}: test_end must be after test_start.")
        return self
