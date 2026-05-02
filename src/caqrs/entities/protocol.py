"""Entity store protocol."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from caqrs.entities.types import (
    Filing,
    IdentifierKind,
    Issuer,
    IssuerId,
    MarketPoint,
    MarketSeries,
    MarketSeriesKind,
    Relation,
    RelationKind,
    Source,
)


class EntityStore(Protocol):
    """The integration layer's public surface."""

    def get_issuer(self, *, issuer_id: IssuerId) -> Issuer | None: ...

    def lookup_issuer(self, *, kind: IdentifierKind, value: str) -> Issuer | None: ...

    def upsert_issuer(self, *, issuer: Issuer) -> Issuer: ...

    def merge_issuers(self, *, keep: IssuerId, drop: IssuerId) -> Issuer: ...

    def get_market_series(
        self,
        *,
        issuer_id: IssuerId,
        kind: MarketSeriesKind,
        range_: tuple[datetime, datetime],
        source_priority: tuple[Source, ...],
    ) -> MarketSeries: ...

    def append_market_points(
        self,
        *,
        issuer_id: IssuerId,
        kind: MarketSeriesKind,
        points: Sequence[MarketPoint],
    ) -> None: ...

    def append_filing(self, *, filing: Filing) -> None: ...

    def filings_for(
        self,
        *,
        issuer_id: IssuerId,
        range_: tuple[datetime, datetime],
        doc_type_codes: Sequence[str] | None = None,
    ) -> tuple[Filing, ...]: ...

    def append_relation(self, *, relation: Relation) -> None: ...

    def subsidiaries_of(
        self,
        *,
        issuer_id: IssuerId,
        at: datetime,
    ) -> tuple[Issuer, ...]: ...

    def relations_for(
        self,
        *,
        issuer_id: IssuerId,
        kind: RelationKind | None = None,
        at: datetime | None = None,
    ) -> tuple[Relation, ...]: ...
