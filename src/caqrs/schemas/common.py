"""Shared types used across all CAQRS artifacts.

Every artifact carries a `RunMetadata` block so cost/latency/lineage analysis
becomes a structured query rather than ad-hoc instrumentation.
"""

import secrets
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

# === Identifiers ===

_RUN_ID_PATTERN: Final[str] = r"^[0-9a-f]{16}$"

RunId = Annotated[
    str,
    Field(
        pattern=_RUN_ID_PATTERN,
        description="Stable identifier for a single agent invocation. 16-char lowercase hex.",
    ),
]


def new_run_id() -> str:
    """Generate a fresh 16-char hex run id (64 bits of entropy)."""
    return secrets.token_hex(8)


# === Bookkeeping primitives ===

UsdAmount = Annotated[
    Decimal,
    Field(ge=0, description="USD amount, non-negative."),
]

LatencyMs = Annotated[
    int,
    Field(ge=0, description="Latency in milliseconds, non-negative."),
]

TokenCount = Annotated[
    int,
    Field(ge=0, description="Token count, non-negative."),
]


# === Domain primitives ===

# Permissive across venues: 1-12 chars, alnum + ".:-_/".
_TICKER_PATTERN: Final[str] = r"^[A-Z0-9][A-Z0-9._:\-/]{0,11}$"

Ticker = Annotated[
    str,
    Field(
        pattern=_TICKER_PATTERN,
        description="Ticker symbol, uppercase. Permissive across venues.",
    ),
]

HorizonDays = Annotated[
    int,
    Field(
        ge=1,
        le=10 * 365,
        description="Forward-looking horizon in days. Bounded to 10 years.",
    ),
]


# === Base classes ===


class StrictBaseModel(BaseModel):
    """Base for all artifact schemas. Frozen + extra=forbid + strict by default."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
    )


class RunMetadata(StrictBaseModel):
    """Per-artifact provenance: who/when/cost/parent.

    Attached to every artifact so regret analysis, cost reports, and ablation
    studies are queryable rather than instrumented.
    """

    run_id: RunId
    parent_id: RunId | None = Field(
        default=None,
        description="Predecessor artifact run_id; None for root.",
    )
    agent_name: str = Field(min_length=1, max_length=80, description="Producing agent name.")
    model_id: str = Field(
        min_length=1,
        max_length=120,
        description="LLM identifier, e.g. 'anthropic/claude-opus-4-7'. 'test' for stubs.",
    )
    created_at: datetime = Field(description="Wall-clock creation time, timezone-aware.")
    llm_cost_usd: UsdAmount = Decimal(0)
    latency_ms: LatencyMs = 0
    token_in: TokenCount = 0
    token_out: TokenCount = 0

    @model_validator(mode="after")
    def _require_tz_aware(self) -> Self:
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware (use datetime.now(UTC)).")
        return self


def utc_now() -> datetime:
    """Tz-aware UTC now, for use as RunMetadata.created_at default in callers."""
    return datetime.now(UTC)
