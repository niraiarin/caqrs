"""In-memory EntityStore implementation."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime

from caqrs.entities.errors import IdentifierConflictError, UnknownIssuerError
from caqrs.entities.types import (
    ConflictRecord,
    Filing,
    Identifier,
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

_IdentifierKey = tuple[IdentifierKind, str]
_MarketKey = tuple[IssuerId, MarketSeriesKind, Source]


class InMemoryEntityStore:
    """Pure-Python EntityStore implementation with no persistence."""

    def __init__(self) -> None:
        self._issuers: dict[IssuerId, Issuer] = {}
        self._identifiers: dict[_IdentifierKey, IssuerId] = {}
        self._market_points: dict[_MarketKey, list[MarketPoint]] = defaultdict(list)
        self._filings: list[Filing] = []
        self._relations: list[Relation] = []

    def get_issuer(self, *, issuer_id: IssuerId) -> Issuer | None:
        return self._issuers.get(issuer_id)

    def lookup_issuer(self, *, kind: IdentifierKind, value: str) -> Issuer | None:
        issuer_id = self._identifiers.get((kind, value))
        if issuer_id is None:
            return None
        return self._issuers[issuer_id]

    def upsert_issuer(self, *, issuer: Issuer) -> Issuer:
        for identifier in issuer.identifiers:
            existing = self._identifiers.get(_identifier_key(identifier))
            if existing is not None and existing != issuer.id:
                raise IdentifierConflictError(
                    kind=identifier.kind,
                    value=identifier.value,
                    existing_issuer_id=existing,
                    proposed_issuer_id=issuer.id,
                )

        previous = self._issuers.get(issuer.id)
        if previous is not None:
            new_keys = {_identifier_key(identifier) for identifier in issuer.identifiers}
            for identifier in previous.identifiers:
                key = _identifier_key(identifier)
                if key not in new_keys and self._identifiers.get(key) == issuer.id:
                    del self._identifiers[key]

        self._issuers[issuer.id] = issuer
        for identifier in issuer.identifiers:
            self._identifiers[_identifier_key(identifier)] = issuer.id
        return issuer

    def merge_issuers(self, *, keep: IssuerId, drop: IssuerId) -> Issuer:
        keep_issuer = self._issuers.get(keep)
        if keep_issuer is None:
            raise UnknownIssuerError(issuer_id=keep)
        if keep == drop:
            return keep_issuer

        drop_issuer = self._issuers.get(drop)
        if drop_issuer is None:
            return keep_issuer

        merged_identifiers = _identifier_union(keep_issuer.identifiers, drop_issuer.identifiers)
        merged = keep_issuer.model_copy(update={"identifiers": merged_identifiers})
        for identifier in drop_issuer.identifiers:
            key = _identifier_key(identifier)
            existing = self._identifiers.get(key)
            if existing is not None and existing not in (keep, drop):
                raise IdentifierConflictError(
                    kind=identifier.kind,
                    value=identifier.value,
                    existing_issuer_id=existing,
                    proposed_issuer_id=keep,
                )
            self._identifiers[key] = keep
        self.upsert_issuer(issuer=merged)

        self._transfer_market_points(keep=keep, drop=drop)
        self._filings = [
            filing.model_copy(update={"issuer_id": keep}) if filing.issuer_id == drop else filing
            for filing in self._filings
        ]
        self._relations = [
            relation.model_copy(
                update={
                    "from_id": keep if relation.from_id == drop else relation.from_id,
                    "to_id": keep if relation.to_id == drop else relation.to_id,
                }
            )
            for relation in self._relations
        ]
        del self._issuers[drop]
        return merged

    def get_market_series(
        self,
        *,
        issuer_id: IssuerId,
        kind: MarketSeriesKind,
        range_: tuple[datetime, datetime],
        source_priority: tuple[Source, ...],
    ) -> MarketSeries:
        start, end = range_
        by_timestamp: dict[datetime, dict[Source, MarketPoint]] = defaultdict(dict)
        for (stored_issuer_id, stored_kind, source), points in self._market_points.items():
            if stored_issuer_id != issuer_id or stored_kind != kind:
                continue
            for point in points:
                if start <= point.timestamp <= end:
                    by_timestamp[point.timestamp][source] = point

        selected: list[MarketPoint] = []
        conflicts: list[ConflictRecord] = []
        for timestamp in sorted(by_timestamp):
            source_points = by_timestamp[timestamp]
            chosen = _choose_point(source_points=source_points, source_priority=source_priority)
            selected.append(chosen)
            discarded = tuple(
                point
                for point in source_points.values()
                if point.provenance.source != chosen.provenance.source
                and point.value != chosen.value
            )
            if discarded:
                conflicts.append(
                    ConflictRecord(timestamp=timestamp, chosen=chosen, discarded=discarded)
                )

        return MarketSeries(
            issuer_id=issuer_id,
            kind=kind,
            points=tuple(selected),
            conflict_log=tuple(conflicts),
        )

    def append_market_points(
        self,
        *,
        issuer_id: IssuerId,
        kind: MarketSeriesKind,
        points: Sequence[MarketPoint],
    ) -> None:
        self._require_issuer(issuer_id=issuer_id)
        for point in points:
            self._market_points[(issuer_id, kind, point.provenance.source)].append(point)

    def append_filing(self, *, filing: Filing) -> None:
        self._require_issuer(issuer_id=filing.issuer_id)
        self._filings.append(filing)

    def filings_for(
        self,
        *,
        issuer_id: IssuerId,
        range_: tuple[datetime, datetime],
        doc_type_codes: Sequence[str] | None = None,
    ) -> tuple[Filing, ...]:
        start, end = range_
        allowed_codes = set(doc_type_codes) if doc_type_codes is not None else None
        return tuple(
            sorted(
                (
                    filing
                    for filing in self._filings
                    if filing.issuer_id == issuer_id
                    and start <= filing.submitted_at <= end
                    and (allowed_codes is None or filing.doc_type_code in allowed_codes)
                ),
                key=lambda filing: filing.submitted_at,
            )
        )

    def append_relation(self, *, relation: Relation) -> None:
        self._require_issuer(issuer_id=relation.from_id)
        self._require_issuer(issuer_id=relation.to_id)
        self._relations.append(relation)

    def subsidiaries_of(
        self,
        *,
        issuer_id: IssuerId,
        at: datetime,
    ) -> tuple[Issuer, ...]:
        subsidiaries = [
            self._issuers[relation.from_id]
            for relation in self._relations
            if relation.kind == RelationKind.SUBSIDIARY_OF
            and relation.to_id == issuer_id
            and _relation_active_at(relation=relation, at=at)
        ]
        return tuple(subsidiaries)

    def relations_for(
        self,
        *,
        issuer_id: IssuerId,
        kind: RelationKind | None = None,
        at: datetime | None = None,
    ) -> tuple[Relation, ...]:
        return tuple(
            relation
            for relation in self._relations
            if issuer_id in (relation.from_id, relation.to_id)
            and (kind is None or relation.kind == kind)
            and (at is None or _relation_active_at(relation=relation, at=at))
        )

    def _require_issuer(self, *, issuer_id: IssuerId) -> None:
        if issuer_id not in self._issuers:
            raise UnknownIssuerError(issuer_id=issuer_id)

    def _transfer_market_points(self, *, keep: IssuerId, drop: IssuerId) -> None:
        transferred: dict[_MarketKey, list[MarketPoint]] = defaultdict(list)
        for (issuer_id, kind, source), points in self._market_points.items():
            target_issuer_id = keep if issuer_id == drop else issuer_id
            transferred[(target_issuer_id, kind, source)].extend(points)
        self._market_points = transferred


def _identifier_key(identifier: Identifier) -> _IdentifierKey:
    return (identifier.kind, identifier.value)


def _identifier_union(
    first: tuple[Identifier, ...],
    second: tuple[Identifier, ...],
) -> tuple[Identifier, ...]:
    by_key = {_identifier_key(identifier): identifier for identifier in first}
    for identifier in second:
        by_key.setdefault(_identifier_key(identifier), identifier)
    return tuple(by_key.values())


def _choose_point(
    *,
    source_points: dict[Source, MarketPoint],
    source_priority: tuple[Source, ...],
) -> MarketPoint:
    for source in source_priority:
        point = source_points.get(source)
        if point is not None:
            return point
    return source_points[sorted(source_points)[0]]


def _relation_active_at(*, relation: Relation, at: datetime) -> bool:
    return relation.valid_from <= at and (relation.valid_to is None or at < relation.valid_to)
