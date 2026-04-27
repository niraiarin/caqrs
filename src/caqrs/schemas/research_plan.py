"""Research Plan: an executable specification for backtesting a hypothesis.

Produced by the Research Agent from a HypothesisCard. Strictly walk-forward.
In-sample-only configurations are forbidden at the schema level: every plan
must contain at least one walk-forward window with disjoint train/test.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from caqrs.schemas.common import RunId, RunMetadata, StrictBaseModel, Ticker


class DataFrequency(StrEnum):
    DAILY = "daily"
    HOURLY = "hourly"
    MINUTE = "minute"


class WalkForwardWindow(StrictBaseModel):
    """A single walk-forward fold. Train and test must be ordered and disjoint."""

    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        for name, ts in (
            ("train_start", self.train_start),
            ("train_end", self.train_end),
            ("test_start", self.test_start),
            ("test_end", self.test_end),
        ):
            if ts.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware.")
        if not (self.train_start < self.train_end <= self.test_start < self.test_end):
            raise ValueError(
                "Window must satisfy train_start < train_end <= test_start < test_end "
                "(no test-set leakage into train).",
            )
        return self


class ResearchPlan(StrictBaseModel):
    schema_version: int = Field(default=1, ge=1, le=1)
    metadata: RunMetadata
    hypothesis_run_id: RunId

    universe: tuple[Ticker, ...] = Field(min_length=1, max_length=500)
    frequency: DataFrequency
    walk_forward: tuple[WalkForwardWindow, ...] = Field(min_length=1, max_length=200)

    cost_model_bps: Annotated[Decimal, Field(ge=0, le=Decimal(1000))]
    slippage_bps: Annotated[Decimal, Field(ge=0, le=Decimal(1000))]

    seed: Annotated[int, Field(ge=0, le=2**63 - 1)]

    @model_validator(mode="after")
    def _no_walk_forward_test_overlap(self) -> Self:
        prev_end: datetime | None = None
        for w in self.walk_forward:
            if prev_end is not None and w.test_start < prev_end:
                raise ValueError(
                    "walk_forward test windows must be ordered and non-overlapping.",
                )
            prev_end = w.test_end
        return self
