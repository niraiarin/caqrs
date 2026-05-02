"""DuckDB-backed EntityStore implementation."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Protocol, cast

import duckdb

from caqrs.entities.errors import EntityStoreError, IdentifierConflictError, UnknownIssuerError
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
    Provenance,
    Relation,
    RelationKind,
    Source,
)

_SCHEMA_VERSION = 1


class _DuckDbResult(Protocol):
    def fetchone(self) -> tuple[object, ...] | None: ...

    def fetchall(self) -> list[tuple[object, ...]]: ...


class _DuckDbConnection(Protocol):
    def close(self) -> None: ...

    def execute(
        self,
        query: str,
        parameters: Sequence[object] | None = None,
    ) -> _DuckDbResult: ...

    def executemany(
        self,
        query: str,
        parameters: Sequence[Sequence[object]],
    ) -> _DuckDbResult: ...


class DuckDbEntityStore:
    """DuckDB-backed EntityStore implementation."""

    def __init__(self, *, path: str | Path | None = None) -> None:
        self._path = ":memory:" if path is None else str(path)
        self._conn = cast(_DuckDbConnection, duckdb.connect(self._path))
        self.init()

    def __enter__(self) -> DuckDbEntityStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def init(self) -> None:
        with self._transaction():
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS entities_schema_version (
                    version INTEGER PRIMARY KEY
                )
                """
            )
            version_row = self._conn.execute(
                "SELECT version FROM entities_schema_version ORDER BY version LIMIT 1"
            ).fetchone()
            version = None if version_row is None else _int_from_db(version_row[0])
            if version is not None and version != _SCHEMA_VERSION:
                raise EntityStoreError(
                    f"unsupported entities schema version {version}; expected {_SCHEMA_VERSION}"
                )

            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS issuers (
                    id VARCHAR PRIMARY KEY,
                    lei VARCHAR,
                    jcn VARCHAR,
                    display_name VARCHAR NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS identifiers (
                    issuer_id VARCHAR NOT NULL,
                    kind VARCHAR NOT NULL,
                    value VARCHAR NOT NULL,
                    PRIMARY KEY (kind, value),
                    FOREIGN KEY (issuer_id) REFERENCES issuers(id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS market_points (
                    issuer_id VARCHAR NOT NULL,
                    kind VARCHAR NOT NULL,
                    timestamp TIMESTAMP NOT NULL,
                    source VARCHAR NOT NULL,
                    value DECIMAL(38, 18) NOT NULL,
                    fetched_at TIMESTAMP NOT NULL,
                    payload_hash VARCHAR NOT NULL,
                    cache_key VARCHAR
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_market_points_issuer_kind_ts
                ON market_points (issuer_id, kind, timestamp)
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS filings (
                    issuer_id VARCHAR NOT NULL,
                    doc_id VARCHAR NOT NULL,
                    doc_type_code VARCHAR NOT NULL,
                    submitted_at TIMESTAMP NOT NULL,
                    parent_doc_id VARCHAR,
                    source VARCHAR NOT NULL,
                    fetched_at TIMESTAMP NOT NULL,
                    payload_hash VARCHAR NOT NULL,
                    cache_key VARCHAR,
                    PRIMARY KEY (doc_id),
                    FOREIGN KEY (issuer_id) REFERENCES issuers(id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS relations (
                    from_id VARCHAR NOT NULL,
                    to_id VARCHAR NOT NULL,
                    kind VARCHAR NOT NULL,
                    valid_from TIMESTAMP NOT NULL,
                    valid_to TIMESTAMP,
                    source VARCHAR NOT NULL,
                    fetched_at TIMESTAMP NOT NULL,
                    payload_hash VARCHAR NOT NULL,
                    cache_key VARCHAR,
                    FOREIGN KEY (from_id) REFERENCES issuers(id),
                    FOREIGN KEY (to_id) REFERENCES issuers(id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_relations_to_kind_validity
                ON relations (to_id, kind, valid_from, valid_to)
                """
            )
            if version_row is None:
                self._conn.execute(
                    "INSERT INTO entities_schema_version (version) VALUES (?)",
                    [_SCHEMA_VERSION],
                )

    def get_issuer(self, *, issuer_id: IssuerId) -> Issuer | None:
        row = self._conn.execute(
            "SELECT id, lei, jcn, display_name FROM issuers WHERE id = ?",
            [issuer_id],
        ).fetchone()
        if row is None:
            return None
        return self._issuer_from_row(row)

    def lookup_issuer(self, *, kind: IdentifierKind, value: str) -> Issuer | None:
        row = self._conn.execute(
            """
            SELECT issuers.id, issuers.lei, issuers.jcn, issuers.display_name
            FROM issuers
            JOIN identifiers ON identifiers.issuer_id = issuers.id
            WHERE identifiers.kind = ? AND identifiers.value = ?
            LIMIT 1
            """,
            [kind.value, value],
        ).fetchone()
        if row is None:
            return None
        return self._issuer_from_row(row)

    def upsert_issuer(self, *, issuer: Issuer) -> Issuer:
        for identifier in issuer.identifiers:
            row = self._conn.execute(
                "SELECT issuer_id FROM identifiers WHERE kind = ? AND value = ?",
                [identifier.kind.value, identifier.value],
            ).fetchone()
            if row is not None and str(row[0]) != issuer.id:
                raise IdentifierConflictError(
                    kind=identifier.kind,
                    value=identifier.value,
                    existing_issuer_id=str(row[0]),
                    proposed_issuer_id=issuer.id,
                )

        with self._transaction():
            exists = self._conn.execute(
                "SELECT 1 FROM issuers WHERE id = ?",
                [issuer.id],
            ).fetchone()
            if exists is None:
                self._conn.execute(
                    "INSERT INTO issuers (id, lei, jcn, display_name) VALUES (?, ?, ?, ?)",
                    [issuer.id, issuer.lei, issuer.jcn, issuer.display_name],
                )
            else:
                self._conn.execute(
                    "UPDATE issuers SET lei = ?, jcn = ?, display_name = ? WHERE id = ?",
                    [issuer.lei, issuer.jcn, issuer.display_name, issuer.id],
                )
            self._conn.execute("DELETE FROM identifiers WHERE issuer_id = ?", [issuer.id])
            if issuer.identifiers:
                self._conn.executemany(
                    "INSERT INTO identifiers (issuer_id, kind, value) VALUES (?, ?, ?)",
                    [
                        (issuer.id, identifier.kind.value, identifier.value)
                        for identifier in issuer.identifiers
                    ],
                )
        return issuer

    def merge_issuers(self, *, keep: IssuerId, drop: IssuerId) -> Issuer:
        keep_issuer = self.get_issuer(issuer_id=keep)
        if keep_issuer is None:
            raise UnknownIssuerError(issuer_id=keep)
        if keep == drop:
            return keep_issuer

        drop_issuer = self.get_issuer(issuer_id=drop)
        if drop_issuer is None:
            return keep_issuer

        merged = keep_issuer.model_copy(
            update={
                "identifiers": _identifier_union(
                    keep_issuer.identifiers,
                    drop_issuer.identifiers,
                )
            }
        )
        with self._transaction():
            self._conn.execute(
                "UPDATE relations SET from_id = ? WHERE from_id = ?",
                [keep, drop],
            )
            self._conn.execute(
                "UPDATE relations SET to_id = ? WHERE to_id = ?",
                [keep, drop],
            )
            self._conn.execute(
                "UPDATE market_points SET issuer_id = ? WHERE issuer_id = ?",
                [keep, drop],
            )
            self._conn.execute(
                "UPDATE filings SET issuer_id = ? WHERE issuer_id = ?",
                [keep, drop],
            )
            self._conn.execute(
                "UPDATE identifiers SET issuer_id = ? WHERE issuer_id = ?",
                [keep, drop],
            )
            self._conn.execute("DELETE FROM issuers WHERE id = ?", [drop])
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
        rows = self._conn.execute(
            """
            SELECT issuer_id, kind, timestamp, value, source, fetched_at, payload_hash, cache_key
            FROM market_points
            WHERE issuer_id = ? AND kind = ? AND timestamp BETWEEN ? AND ?
            ORDER BY timestamp, source
            """,
            [issuer_id, kind.value, _to_db_datetime(start), _to_db_datetime(end)],
        ).fetchall()

        by_timestamp: dict[datetime, dict[Source, MarketPoint]] = defaultdict(dict)
        for row in rows:
            point = _market_point_from_row(row)
            by_timestamp[point.timestamp][point.provenance.source] = point

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
        if not points:
            return
        self._conn.executemany(
            """
            INSERT INTO market_points (
                issuer_id, kind, timestamp, source, value, fetched_at, payload_hash, cache_key
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    issuer_id,
                    kind.value,
                    _to_db_datetime(point.timestamp),
                    point.provenance.source.value,
                    point.value,
                    _to_db_datetime(point.provenance.fetched_at),
                    point.provenance.payload_hash,
                    point.provenance.cache_key,
                )
                for point in points
            ],
        )

    def append_filing(self, *, filing: Filing) -> None:
        self._require_issuer(issuer_id=filing.issuer_id)
        self._conn.execute(
            """
            INSERT INTO filings (
                issuer_id, doc_id, doc_type_code, submitted_at, parent_doc_id,
                source, fetched_at, payload_hash, cache_key
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                filing.issuer_id,
                filing.doc_id,
                filing.doc_type_code,
                _to_db_datetime(filing.submitted_at),
                filing.parent_doc_id,
                filing.provenance.source.value,
                _to_db_datetime(filing.provenance.fetched_at),
                filing.provenance.payload_hash,
                filing.provenance.cache_key,
            ],
        )

    def filings_for(
        self,
        *,
        issuer_id: IssuerId,
        range_: tuple[datetime, datetime],
        doc_type_codes: Sequence[str] | None = None,
    ) -> tuple[Filing, ...]:
        start, end = range_
        params: list[object] = [issuer_id, _to_db_datetime(start), _to_db_datetime(end)]
        query = """
            SELECT issuer_id, doc_id, doc_type_code, submitted_at, parent_doc_id,
                   source, fetched_at, payload_hash, cache_key
            FROM filings
            WHERE issuer_id = ? AND submitted_at BETWEEN ? AND ?
        """
        if doc_type_codes is not None and len(doc_type_codes) == 0:
            return ()
        if doc_type_codes is not None:
            placeholders = ", ".join("?" for _ in doc_type_codes)
            query += f" AND doc_type_code IN ({placeholders})"
            params.extend(doc_type_codes)
        query += " ORDER BY submitted_at"

        return tuple(_filing_from_row(row) for row in self._conn.execute(query, params).fetchall())

    def append_relation(self, *, relation: Relation) -> None:
        self._require_issuer(issuer_id=relation.from_id)
        self._require_issuer(issuer_id=relation.to_id)
        self._conn.execute(
            """
            INSERT INTO relations (
                from_id, to_id, kind, valid_from, valid_to,
                source, fetched_at, payload_hash, cache_key
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                relation.from_id,
                relation.to_id,
                relation.kind.value,
                _to_db_datetime(relation.valid_from),
                _to_db_datetime(relation.valid_to),
                relation.provenance.source.value,
                _to_db_datetime(relation.provenance.fetched_at),
                relation.provenance.payload_hash,
                relation.provenance.cache_key,
            ],
        )

    def subsidiaries_of(
        self,
        *,
        issuer_id: IssuerId,
        at: datetime,
    ) -> tuple[Issuer, ...]:
        rows = self._conn.execute(
            """
            SELECT issuers.id, issuers.lei, issuers.jcn, issuers.display_name
            FROM relations
            JOIN issuers ON issuers.id = relations.from_id
            WHERE relations.to_id = ?
              AND relations.kind = ?
              AND relations.valid_from <= ?
              AND (relations.valid_to IS NULL OR relations.valid_to > ?)
            """,
            [
                issuer_id,
                RelationKind.SUBSIDIARY_OF.value,
                _to_db_datetime(at),
                _to_db_datetime(at),
            ],
        ).fetchall()
        return tuple(self._issuer_from_row(row) for row in rows)

    def relations_for(
        self,
        *,
        issuer_id: IssuerId,
        kind: RelationKind | None = None,
        at: datetime | None = None,
    ) -> tuple[Relation, ...]:
        params: list[object] = [issuer_id, issuer_id]
        query = """
            SELECT from_id, to_id, kind, valid_from, valid_to,
                   source, fetched_at, payload_hash, cache_key
            FROM relations
            WHERE (from_id = ? OR to_id = ?)
        """
        if kind is not None:
            query += " AND kind = ?"
            params.append(kind.value)
        if at is not None:
            query += " AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)"
            params.extend((_to_db_datetime(at), _to_db_datetime(at)))
        rows = self._conn.execute(query, params).fetchall()
        return tuple(_relation_from_row(row) for row in rows)

    def list_all_issuers(self) -> tuple[Issuer, ...]:
        """Implementation-specific helper for round-trip tests and ad-hoc inspection."""
        rows = self._conn.execute(
            "SELECT id, lei, jcn, display_name FROM issuers ORDER BY id"
        ).fetchall()
        return tuple(self._issuer_from_row(row) for row in rows)

    def list_all_filings(self) -> tuple[Filing, ...]:
        """Implementation-specific helper for round-trip tests and ad-hoc inspection."""
        rows = self._conn.execute(
            """
            SELECT issuer_id, doc_id, doc_type_code, submitted_at, parent_doc_id,
                   source, fetched_at, payload_hash, cache_key
            FROM filings
            ORDER BY doc_id
            """
        ).fetchall()
        return tuple(_filing_from_row(row) for row in rows)

    def list_all_relations(self) -> tuple[Relation, ...]:
        """Implementation-specific helper for round-trip tests and ad-hoc inspection."""
        rows = self._conn.execute(
            """
            SELECT from_id, to_id, kind, valid_from, valid_to,
                   source, fetched_at, payload_hash, cache_key
            FROM relations
            ORDER BY from_id, to_id, kind, valid_from
            """
        ).fetchall()
        return tuple(_relation_from_row(row) for row in rows)

    def _issuer_from_row(self, row: tuple[object, ...]) -> Issuer:
        issuer_id = str(row[0])
        identifier_rows = self._conn.execute(
            """
            SELECT kind, value
            FROM identifiers
            WHERE issuer_id = ?
            ORDER BY kind, value
            """,
            [issuer_id],
        ).fetchall()
        return Issuer(
            id=issuer_id,
            lei=None if row[1] is None else str(row[1]),
            jcn=None if row[2] is None else str(row[2]),
            display_name=str(row[3]),
            identifiers=tuple(
                Identifier(kind=IdentifierKind(str(kind)), value=str(value))
                for kind, value in identifier_rows
            ),
        )

    def _require_issuer(self, *, issuer_id: IssuerId) -> None:
        if self.get_issuer(issuer_id=issuer_id) is None:
            raise UnknownIssuerError(issuer_id=issuer_id)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._conn.execute("BEGIN TRANSACTION")
        try:
            yield
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")


def _identifier_key(identifier: Identifier) -> tuple[IdentifierKind, str]:
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


def _to_db_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.astimezone(UTC).replace(tzinfo=None)


def _from_db_datetime(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"expected datetime from DuckDB, got {type(value).__name__}")
    if value.tzinfo is not None:
        return value.astimezone(UTC)
    return value.replace(tzinfo=UTC)


def _provenance_from_row(row: tuple[object, ...], *, offset: int) -> Provenance:
    return Provenance(
        source=Source(str(row[offset])),
        fetched_at=_from_db_datetime(row[offset + 1]),
        payload_hash=str(row[offset + 2]),
        cache_key=None if row[offset + 3] is None else str(row[offset + 3]),
    )


def _market_point_from_row(row: tuple[object, ...]) -> MarketPoint:
    return MarketPoint(
        timestamp=_from_db_datetime(row[2]),
        value=_decimal_from_db(row[3]),
        provenance=_provenance_from_row(row, offset=4),
    )


def _decimal_from_db(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str | int):
        return Decimal(value)
    raise TypeError(f"expected decimal-compatible value from DuckDB, got {type(value).__name__}")


def _int_from_db(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise TypeError(f"expected integer-compatible value from DuckDB, got {type(value).__name__}")


def _filing_from_row(row: tuple[object, ...]) -> Filing:
    return Filing(
        issuer_id=str(row[0]),
        doc_id=str(row[1]),
        doc_type_code=str(row[2]),
        submitted_at=_from_db_datetime(row[3]),
        parent_doc_id=None if row[4] is None else str(row[4]),
        provenance=_provenance_from_row(row, offset=5),
    )


def _relation_from_row(row: tuple[object, ...]) -> Relation:
    return Relation(
        from_id=str(row[0]),
        to_id=str(row[1]),
        kind=RelationKind(str(row[2])),
        valid_from=_from_db_datetime(row[3]),
        valid_to=None if row[4] is None else _from_db_datetime(row[4]),
        provenance=_provenance_from_row(row, offset=5),
    )
