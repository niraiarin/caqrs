"""Hypothesis Card: a falsifiable claim about market behavior with acceptance criteria.

The Hypothesis Agent emits these. The Skeptic Agent attempts to falsify them.
The Research Agent translates them into a `ResearchPlan` for backtest.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from caqrs.schemas.common import HorizonDays, RunMetadata, StrictBaseModel, Ticker


class HypothesisStatus(StrEnum):
    DRAFT = "draft"
    UNDER_REVIEW = "under_review"
    ADOPTED = "adopted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"
    LONG_SHORT = "long_short"
    NEUTRAL = "neutral"


class AcceptanceCriterion(StrictBaseModel):
    """A single quantitative criterion the hypothesis must clear.

    Checked against `BacktestReport` after backtest. `metric_path` is a dotted
    path within the report (e.g. ``aggregate.median_sharpe``).
    """

    metric_path: str = Field(min_length=1, max_length=200)
    op: str = Field(pattern=r"^(>=|<=|>|<|==|!=)$")
    threshold: Decimal


class HypothesisCard(StrictBaseModel):
    """A falsifiable claim with bounded scope and acceptance criteria.

    The claim is intentionally constrained to a single sentence to keep the
    Skeptic's adversarial job tractable.
    """

    schema_version: int = Field(default=1, ge=1, le=1)
    metadata: RunMetadata
    status: HypothesisStatus

    claim: str = Field(min_length=10, max_length=500)
    rationale: str = Field(min_length=1, max_length=4000)

    universe: tuple[Ticker, ...] = Field(min_length=1, max_length=500)
    direction: Direction
    horizon_days: HorizonDays

    variables: tuple[str, ...] = Field(min_length=1, max_length=20)
    acceptance: tuple[AcceptanceCriterion, ...] = Field(min_length=1, max_length=10)

    max_drawdown_pct: Annotated[Decimal, Field(gt=0, le=Decimal(100))]
    expected_window_start: datetime
    expected_window_end: datetime

    @model_validator(mode="after")
    def _check_window(self) -> Self:
        if self.expected_window_start.tzinfo is None or self.expected_window_end.tzinfo is None:
            raise ValueError("expected_window_{start,end} must be timezone-aware.")
        if self.expected_window_end <= self.expected_window_start:
            raise ValueError("expected_window_end must be strictly after expected_window_start.")
        return self

    @model_validator(mode="after")
    def _no_duplicate_universe(self) -> Self:
        if len(set(self.universe)) != len(self.universe):
            raise ValueError("universe must not contain duplicates.")
        return self
