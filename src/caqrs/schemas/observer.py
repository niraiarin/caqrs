"""Observer-agent input and output artifacts.

The Observer is the entry point of a research cycle: given a universe
and a horizon, it gathers per-asset metrics, summarises the current
regime, surfaces news themes, and notes any data quality issues. The
output (``ObserverArtifact``) feeds the Hypothesis agent.

``ObserverInput`` is given by the operator or orchestrator; it has no
``RunMetadata`` because it is a request, not an emitted artifact.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from caqrs.schemas.common import HorizonDays, RunMetadata, StrictBaseModel, Ticker


class DataDimension(StrEnum):
    """What the Observer should gather."""

    PRICES = "prices"
    NEWS = "news"
    MACRO = "macro"
    FILINGS = "filings"
    SOCIAL = "social"


class ObserverInput(StrictBaseModel):
    """Request to the Observer agent. Not itself an emitted artifact."""

    schema_version: int = Field(default=1, ge=1, le=1)

    universe: tuple[Ticker, ...] = Field(min_length=1, max_length=500)
    as_of: datetime
    horizon_days: HorizonDays
    dimensions: tuple[DataDimension, ...] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def _require_tz_aware(self) -> Self:
        if self.as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware.")
        return self

    @model_validator(mode="after")
    def _no_duplicate_universe(self) -> Self:
        if len(set(self.universe)) != len(self.universe):
            raise ValueError("universe must not contain duplicates.")
        return self

    @model_validator(mode="after")
    def _no_duplicate_dimensions(self) -> Self:
        if len(set(self.dimensions)) != len(self.dimensions):
            raise ValueError("dimensions must not contain duplicates.")
        return self


class AssetSnapshot(StrictBaseModel):
    """Per-asset summary stats. Raw price series live in the memory layer."""

    ticker: Ticker
    return_1m: Decimal | None = None
    return_12m: Decimal | None = None
    volatility_30d: Annotated[Decimal, Field(ge=0)] | None = None
    last_close: Annotated[Decimal, Field(gt=0)] | None = None
    note: str | None = Field(default=None, max_length=500)


class ObserverArtifact(StrictBaseModel):
    """Output of the Observer agent. Feeds the Hypothesis agent."""

    schema_version: int = Field(default=1, ge=1, le=1)
    metadata: RunMetadata

    universe: tuple[Ticker, ...] = Field(min_length=1, max_length=500)
    as_of: datetime
    regime_summary: str = Field(min_length=1, max_length=2000)

    asset_snapshots: tuple[AssetSnapshot, ...] = Field(default=(), max_length=500)
    news_themes: tuple[str, ...] = Field(default=(), max_length=20)
    macro_notes: str = Field(default="", max_length=4000)
    data_quality_notes: tuple[str, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def _require_tz_aware(self) -> Self:
        if self.as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware.")
        return self

    @model_validator(mode="after")
    def _no_duplicate_universe(self) -> Self:
        if len(set(self.universe)) != len(self.universe):
            raise ValueError("universe must not contain duplicates.")
        return self

    @model_validator(mode="after")
    def _snapshots_within_universe(self) -> Self:
        snap_tickers = [s.ticker for s in self.asset_snapshots]
        if len(set(snap_tickers)) != len(snap_tickers):
            raise ValueError("asset_snapshots must not contain duplicate tickers.")
        universe_set = set(self.universe)
        for ticker in snap_tickers:
            if ticker not in universe_set:
                raise ValueError(
                    f"asset_snapshots contains {ticker!r} which is not in universe.",
                )
        return self
