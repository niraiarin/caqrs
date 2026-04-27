"""Strategy Decision: the policy gateway emit point.

Decision artifacts are the *only* path to broker execution. In P0-P2 the
decision is emitted but execution is suppressed; the Policy Gateway projection
arrives in P3 (asset / position / loss-limit projections applied here).
"""

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from caqrs.schemas.common import RunId, RunMetadata, StrictBaseModel, Ticker


class DecisionAction(StrEnum):
    ADOPT = "adopt"
    REJECT = "reject"
    DEFER = "defer"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class TargetPosition(StrictBaseModel):
    ticker: Ticker
    side: Side
    weight: Annotated[Decimal, Field(gt=0, le=Decimal(1))]


class StrategyDecision(StrictBaseModel):
    schema_version: int = Field(default=1, ge=1, le=1)
    metadata: RunMetadata
    backtest_run_id: RunId

    action: DecisionAction
    targets: tuple[TargetPosition, ...] = Field(default=(), max_length=500)
    rationale: str = Field(min_length=1, max_length=4000)

    notional_cap_usd: Annotated[Decimal, Field(ge=0)]
    max_position_weight: Annotated[Decimal, Field(gt=0, le=Decimal(1))]
    daily_loss_limit_usd: Annotated[Decimal, Field(ge=0)]

    @model_validator(mode="after")
    def _adopt_requires_targets(self) -> Self:
        if self.action == DecisionAction.ADOPT and not self.targets:
            raise ValueError("action=adopt requires at least one target position.")
        if self.action != DecisionAction.ADOPT and self.targets:
            raise ValueError("targets must be empty unless action=adopt.")
        return self

    @model_validator(mode="after")
    def _weights_within_envelope(self) -> Self:
        for target in self.targets:
            if target.weight > self.max_position_weight:
                raise ValueError(
                    f"position weight {target.weight} for {target.ticker} exceeds "
                    f"max_position_weight {self.max_position_weight}.",
                )
        total = sum((target.weight for target in self.targets), start=Decimal(0))
        if total > Decimal(1):
            raise ValueError(
                f"sum of position weights {total} exceeds 1 "
                "(cash-only baseline requires fully invested ≤ 100%).",
            )
        return self

    @model_validator(mode="after")
    def _no_duplicate_tickers(self) -> Self:
        seen = [target.ticker for target in self.targets]
        if len(set(seen)) != len(seen):
            raise ValueError("targets must not contain duplicate tickers.")
        return self
