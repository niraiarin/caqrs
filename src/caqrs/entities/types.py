"""Core entity types for canonical issuer integration."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, field_validator, model_validator

from caqrs.schemas.common import StrictBaseModel

IssuerId = Annotated[str, Field(pattern=r"^I[0-9a-f]{16}$")]


def new_issuer_id() -> IssuerId:
    """Generate a fresh canonical issuer id."""
    return f"I{secrets.token_hex(8)}"


def _normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


class Source(StrEnum):
    """Origin of a CAQRS-ingested row."""

    JQUANTS = "jquants"
    YFINANCE = "yfinance"
    POLYMARKET_CLOB = "polymarket_clob"
    POLYMARKET_GAMMA = "polymarket_gamma"
    POLYMARKET_ARCHIVE = "polymarket_archive"
    EDINET = "edinet"
    EDINETDB = "edinetdb"


class IdentifierKind(StrEnum):
    """Distinct identifier namespaces."""

    JQUANTS_CODE = "jquants_code"
    SEC_CODE = "sec_code"
    YFINANCE_TICKER = "yfinance_ticker"
    EDINET_CODE = "edinet_code"
    JCN = "jcn"
    LEI = "lei"
    POLYMARKET_TOKEN = "polymarket_token"


class Identifier(StrictBaseModel):
    """One source-specific id pointing at one canonical Issuer."""

    kind: IdentifierKind
    value: str = Field(min_length=1, max_length=64)


class Issuer(StrictBaseModel):
    """Canonical legal-entity record."""

    id: IssuerId
    lei: str | None = Field(default=None, pattern=r"^[A-Z0-9]{20}$")
    jcn: str | None = Field(default=None, pattern=r"^\d{13}$")
    display_name: str = Field(min_length=1, max_length=256)
    identifiers: tuple[Identifier, ...]


class Provenance(StrictBaseModel):
    """Why the agent should believe this row."""

    source: Source
    fetched_at: datetime
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    cache_key: str | None = None

    @field_validator("fetched_at")
    @classmethod
    def _require_utc(cls, value: datetime) -> datetime:
        return _normalize_utc(value)


class MarketSeriesKind(StrEnum):
    """Market observation namespaces."""

    DAILY_OPEN = "daily_open"
    DAILY_HIGH = "daily_high"
    DAILY_LOW = "daily_low"
    DAILY_CLOSE = "daily_close"
    DAILY_ADJ_CLOSE = "daily_adj_close"
    DAILY_VOLUME = "daily_volume"
    POLYMARKET_MIDPOINT = "polymarket_midpoint"


class MarketPoint(StrictBaseModel):
    """One sourced market observation."""

    timestamp: datetime
    value: Decimal
    provenance: Provenance

    @field_validator("timestamp")
    @classmethod
    def _require_utc(cls, value: datetime) -> datetime:
        return _normalize_utc(value)


class ConflictRecord(StrictBaseModel):
    """A source-priority choice where sources disagreed exactly on value."""

    timestamp: datetime
    chosen: MarketPoint
    discarded: tuple[MarketPoint, ...]

    @field_validator("timestamp")
    @classmethod
    def _require_utc(cls, value: datetime) -> datetime:
        return _normalize_utc(value)


class MarketSeries(StrictBaseModel):
    """Market observations for one issuer and kind."""

    issuer_id: IssuerId
    kind: MarketSeriesKind
    points: tuple[MarketPoint, ...]
    conflict_log: tuple[ConflictRecord, ...] = ()


class Filing(StrictBaseModel):
    """One disclosure event keyed by its EDINET docID."""

    issuer_id: IssuerId
    doc_id: str = Field(min_length=1, max_length=20)
    doc_type_code: str
    submitted_at: datetime
    parent_doc_id: str | None = None
    provenance: Provenance

    @field_validator("submitted_at")
    @classmethod
    def _require_utc(cls, value: datetime) -> datetime:
        return _normalize_utc(value)


class RelationKind(StrEnum):
    """Closed enum for supported issuer relation edges."""

    SUBSIDIARY_OF = "subsidiary_of"
    LARGE_SHAREHOLDER_OF = "large_shareholder_of"
    PUBLIC_TENDER_TARGET = "public_tender_target"
    POLYMARKET_SUBJECT = "polymarket_subject"


class Relation(StrictBaseModel):
    """A time-bounded edge between two Issuers."""

    from_id: IssuerId
    to_id: IssuerId
    kind: RelationKind
    valid_from: datetime
    valid_to: datetime | None = None
    provenance: Provenance

    @field_validator("valid_from", "valid_to")
    @classmethod
    def _require_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _normalize_utc(value)

    @model_validator(mode="after")
    def _require_ordered_interval(self) -> Self:
        if self.valid_to is not None and self.valid_from >= self.valid_to:
            raise ValueError("valid_from must be earlier than valid_to")
        return self
