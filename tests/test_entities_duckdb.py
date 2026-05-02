"""Tests for DuckDbEntityStore."""

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic_core import to_json

from caqrs.entities import (
    ConflictRecord,
    Filing,
    Identifier,
    IdentifierConflictError,
    IdentifierKind,
    Issuer,
    MarketPoint,
    MarketSeriesKind,
    Provenance,
    Relation,
    RelationKind,
    Source,
    UnknownIssuerError,
)
from caqrs.entities.duckdb import DuckDbEntityStore

_TOYOTA_ID = "I0000000000000001"
_SONY_ID = "I0000000000000002"
_A_ID = "I000000000000000a"
_B_ID = "I000000000000000b"
_C_ID = "I000000000000000c"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDbEntityStore]:
    with DuckDbEntityStore(path=tmp_path / "store.duckdb") as entity_store:
        yield entity_store


@pytest.mark.traces("ENT-A1", "ENT-T1")
def test_empty_store_lookup_returns_none(store: DuckDbEntityStore) -> None:
    assert store.lookup_issuer(kind=IdentifierKind.JQUANTS_CODE, value="72030") is None


@pytest.mark.traces("ENT-A1", "ENT-T2")
def test_lookup_by_each_toyota_identifier_returns_same_issuer(
    store: DuckDbEntityStore,
) -> None:
    issuer = _toyota()
    store.upsert_issuer(issuer=issuer)

    assert store.lookup_issuer(kind=IdentifierKind.JQUANTS_CODE, value="72030") == issuer
    assert store.lookup_issuer(kind=IdentifierKind.EDINET_CODE, value="E02144") == issuer
    assert store.lookup_issuer(kind=IdentifierKind.YFINANCE_TICKER, value="7203.T") == issuer


@pytest.mark.traces("ENT-A2", "ENT-T3")
def test_lookup_by_jcn_then_lei_returns_equal_issuer(store: DuckDbEntityStore) -> None:
    issuer = _toyota()
    store.upsert_issuer(issuer=issuer)

    via_jcn = store.lookup_issuer(kind=IdentifierKind.JCN, value="1180301018771")
    via_lei = store.lookup_issuer(kind=IdentifierKind.LEI, value="5493006Z4DXP3JNCAY09")

    assert via_jcn == via_lei == issuer


@pytest.mark.traces("ENT-A3", "ENT-T4")
def test_upsert_rejects_identifier_registered_to_different_issuer(
    store: DuckDbEntityStore,
) -> None:
    store.upsert_issuer(issuer=_toyota())
    other = Issuer(
        id="I0000000000000003",
        lei=None,
        jcn=None,
        display_name="Other",
        identifiers=(Identifier(kind=IdentifierKind.JQUANTS_CODE, value="72030"),),
    )

    with pytest.raises(IdentifierConflictError):
        store.upsert_issuer(issuer=other)


@pytest.mark.traces("ENT-A3", "ENT-T4")
def test_identifier_conflict_error_exposes_payload_fields(
    store: DuckDbEntityStore,
) -> None:
    toyota = _toyota()
    store.upsert_issuer(issuer=toyota)
    other_issuer = Issuer(
        id="I0000000000000003",
        lei=None,
        jcn=None,
        display_name="Other",
        identifiers=(Identifier(kind=IdentifierKind.JQUANTS_CODE, value="72030"),),
    )

    with pytest.raises(IdentifierConflictError) as exc_info:
        store.upsert_issuer(issuer=other_issuer)

    error = exc_info.value
    assert error.kind == IdentifierKind.JQUANTS_CODE
    assert error.value == "72030"
    assert error.existing_issuer_id == toyota.id
    assert error.proposed_issuer_id == other_issuer.id


@pytest.mark.traces("ENT-A4", "ENT-T5")
def test_market_series_uses_jquants_when_priority_covers_whole_range(
    store: DuckDbEntityStore,
) -> None:
    _store_with_toyota(store)
    points = _daily_points(
        source=Source.JQUANTS,
        start=datetime(2025, 6, 1, tzinfo=UTC),
        count=30,
        base=Decimal("100"),
    )
    same_values = _daily_points(
        source=Source.YFINANCE,
        start=datetime(2025, 6, 1, tzinfo=UTC),
        count=30,
        base=Decimal("100"),
    )
    store.append_market_points(
        issuer_id=_TOYOTA_ID,
        kind=MarketSeriesKind.DAILY_CLOSE,
        points=points,
    )
    store.append_market_points(
        issuer_id=_TOYOTA_ID,
        kind=MarketSeriesKind.DAILY_CLOSE,
        points=same_values,
    )

    series = store.get_market_series(
        issuer_id=_TOYOTA_ID,
        kind=MarketSeriesKind.DAILY_CLOSE,
        range_=(datetime(2025, 6, 1, tzinfo=UTC), datetime(2025, 6, 30, tzinfo=UTC)),
        source_priority=(Source.JQUANTS, Source.YFINANCE),
    )

    assert len(series.points) == 30
    assert {point.provenance.source for point in series.points} == {Source.JQUANTS}
    assert series.conflict_log == ()


@pytest.mark.traces("ENT-A4", "ENT-T6")
def test_market_series_falls_back_to_yfinance_for_uncovered_days(
    store: DuckDbEntityStore,
) -> None:
    _store_with_toyota(store)
    jquants_points = _daily_points(
        source=Source.JQUANTS,
        start=datetime(2025, 6, 1, tzinfo=UTC),
        count=30,
        base=Decimal("100"),
    )
    adr_only = _daily_points(
        source=Source.YFINANCE,
        start=datetime(2025, 7, 1, tzinfo=UTC),
        count=5,
        base=Decimal("200"),
    )
    store.append_market_points(
        issuer_id=_TOYOTA_ID,
        kind=MarketSeriesKind.DAILY_CLOSE,
        points=jquants_points,
    )
    store.append_market_points(
        issuer_id=_TOYOTA_ID,
        kind=MarketSeriesKind.DAILY_CLOSE,
        points=adr_only,
    )

    series = store.get_market_series(
        issuer_id=_TOYOTA_ID,
        kind=MarketSeriesKind.DAILY_CLOSE,
        range_=(datetime(2025, 6, 1, tzinfo=UTC), datetime(2025, 7, 5, tzinfo=UTC)),
        source_priority=(Source.JQUANTS, Source.YFINANCE),
    )

    assert len(series.points) == 35
    assert [point.provenance.source for point in series.points[-5:]] == [Source.YFINANCE] * 5
    assert series.conflict_log == ()


@pytest.mark.traces("ENT-A4", "ENT-T6")
def test_t6_conflict_log_records_disagreed_points(store: DuckDbEntityStore) -> None:
    _store_with_toyota(store)
    timestamp = datetime(2025, 6, 2, tzinfo=UTC)
    higher_priority = _point(Source.JQUANTS, timestamp, Decimal("100"))
    lower_priority = _point(Source.YFINANCE, timestamp, Decimal("101"))
    store.append_market_points(
        issuer_id=_TOYOTA_ID,
        kind=MarketSeriesKind.DAILY_CLOSE,
        points=(higher_priority,),
    )
    store.append_market_points(
        issuer_id=_TOYOTA_ID,
        kind=MarketSeriesKind.DAILY_CLOSE,
        points=(lower_priority,),
    )

    series = store.get_market_series(
        issuer_id=_TOYOTA_ID,
        kind=MarketSeriesKind.DAILY_CLOSE,
        range_=(timestamp, timestamp),
        source_priority=(Source.JQUANTS, Source.YFINANCE),
    )

    assert series.conflict_log == (
        ConflictRecord(
            timestamp=timestamp,
            chosen=higher_priority,
            discarded=(lower_priority,),
        ),
    )


@pytest.mark.traces("ENT-A10")
def test_append_market_points_rejects_unknown_issuer(store: DuckDbEntityStore) -> None:
    with pytest.raises(UnknownIssuerError):
        store.append_market_points(
            issuer_id=_TOYOTA_ID,
            kind=MarketSeriesKind.DAILY_CLOSE,
            points=(_point(Source.JQUANTS, datetime(2025, 6, 1, tzinfo=UTC), Decimal("1")),),
        )


def test_append_filing_with_unknown_issuer_raises(store: DuckDbEntityStore) -> None:
    filing = _filing(doc_id="S1000001", submitted_at=datetime(2025, 6, 1, 1, tzinfo=UTC))

    with pytest.raises(UnknownIssuerError):
        store.append_filing(filing=filing)


@pytest.mark.traces("ENT-A5", "ENT-T7")
def test_append_filing_then_filings_for_returns_filing(store: DuckDbEntityStore) -> None:
    _store_with_toyota(store)
    filing = _filing(doc_id="S1000001", submitted_at=datetime(2025, 6, 1, 1, tzinfo=UTC))
    store.append_filing(filing=filing)

    assert store.filings_for(
        issuer_id=_TOYOTA_ID,
        range_=(datetime(2025, 6, 1, tzinfo=UTC), datetime(2025, 6, 2, tzinfo=UTC)),
        doc_type_codes=("120",),
    ) == (filing,)


@pytest.mark.traces("ENT-A5", "ENT-T8")
def test_corrective_filing_is_returned_in_submit_time_order(
    store: DuckDbEntityStore,
) -> None:
    _store_with_toyota(store)
    original = _filing(doc_id="S1000001", submitted_at=datetime(2025, 6, 1, 1, tzinfo=UTC))
    correction = _filing(
        doc_id="S1000002",
        submitted_at=datetime(2025, 6, 1, 2, tzinfo=UTC),
        parent_doc_id="S1000001",
    )
    store.append_filing(filing=correction)
    store.append_filing(filing=original)

    filings = store.filings_for(
        issuer_id=_TOYOTA_ID,
        range_=(datetime(2025, 6, 1, tzinfo=UTC), datetime(2025, 6, 2, tzinfo=UTC)),
    )

    assert filings == (original, correction)
    assert filings[1].parent_doc_id == original.doc_id


@pytest.mark.traces("ENT-A6", "ENT-T9")
def test_subsidiaries_of_returns_relation_active_on_last_effective_day(
    store: DuckDbEntityStore,
) -> None:
    _store_with_relation_graph(store)

    assert store.subsidiaries_of(
        issuer_id=_B_ID,
        at=datetime(2024, 3, 31, tzinfo=UTC),
    ) == (store.get_issuer(issuer_id=_A_ID),)


@pytest.mark.traces("ENT-A6", "ENT-T10")
def test_subsidiaries_of_excludes_relation_on_exclusive_end_date(
    store: DuckDbEntityStore,
) -> None:
    _store_with_relation_graph(store)

    assert (
        store.subsidiaries_of(
            issuer_id=_B_ID,
            at=datetime(2024, 4, 1, tzinfo=UTC),
        )
        == ()
    )


@pytest.mark.traces("ENT-A11")
def test_append_relation_with_unknown_issuer_raises(store: DuckDbEntityStore) -> None:
    store.upsert_issuer(issuer=_issuer(_A_ID, "A"))
    relation = Relation(
        from_id=_A_ID,
        to_id=_B_ID,
        kind=RelationKind.SUBSIDIARY_OF,
        valid_from=datetime(2020, 4, 1, tzinfo=UTC),
        valid_to=datetime(2024, 4, 1, tzinfo=UTC),
        provenance=_provenance(Source.EDINET),
    )

    with pytest.raises(UnknownIssuerError):
        store.append_relation(relation=relation)


@pytest.mark.traces("ENT-A8", "ENT-T12")
def test_market_point_provenance_preserves_jquants_payload_hash(
    store: DuckDbEntityStore,
) -> None:
    _store_with_toyota(store)
    original_response = {"daily_quotes": [{"Code": "72030", "Date": "2025-06-02", "Close": "1"}]}
    payload_hash = hashlib.sha256(to_json(original_response)).hexdigest()
    fetched_at = datetime(2025, 6, 3, tzinfo=UTC)
    point = MarketPoint(
        timestamp=datetime(2025, 6, 2, tzinfo=UTC),
        value=Decimal("1"),
        provenance=Provenance(
            source=Source.JQUANTS,
            fetched_at=fetched_at,
            payload_hash=payload_hash,
        ),
    )
    store.append_market_points(
        issuer_id=_TOYOTA_ID,
        kind=MarketSeriesKind.DAILY_CLOSE,
        points=(point,),
    )

    series = store.get_market_series(
        issuer_id=_TOYOTA_ID,
        kind=MarketSeriesKind.DAILY_CLOSE,
        range_=(datetime(2025, 6, 1, tzinfo=UTC), datetime(2025, 6, 30, tzinfo=UTC)),
        source_priority=(Source.JQUANTS,),
    )

    assert series.points[0].provenance.source == Source.JQUANTS
    assert series.points[0].provenance.payload_hash == payload_hash
    assert series.points[0].provenance.fetched_at == fetched_at


@pytest.mark.traces("ENT-A7", "ENT-T11")
def test_t11_round_trip_persists_graph_for_fresh_store(tmp_path: Path) -> None:
    path = tmp_path / "store.duckdb"
    toyota = _toyota()
    sony = Issuer(
        id=_SONY_ID,
        lei=None,
        jcn=None,
        display_name="Sony Group Corporation",
        identifiers=(Identifier(kind=IdentifierKind.YFINANCE_TICKER, value="6758.T"),),
    )
    filings = (_filing(doc_id="S1000001", submitted_at=datetime(2025, 6, 1, 1, tzinfo=UTC)),)
    relations = (
        Relation(
            from_id=_SONY_ID,
            to_id=_TOYOTA_ID,
            kind=RelationKind.LARGE_SHAREHOLDER_OF,
            valid_from=datetime(2025, 1, 1, tzinfo=UTC),
            valid_to=None,
            provenance=_provenance(Source.EDINET),
        ),
        Relation(
            from_id=_TOYOTA_ID,
            to_id=_SONY_ID,
            kind=RelationKind.POLYMARKET_SUBJECT,
            valid_from=datetime(2025, 2, 1, tzinfo=UTC),
            valid_to=datetime(2025, 3, 1, tzinfo=UTC),
            provenance=_provenance(Source.POLYMARKET_GAMMA),
        ),
    )

    with DuckDbEntityStore(path=path) as first:
        first.upsert_issuer(issuer=toyota)
        first.upsert_issuer(issuer=sony)
        first.append_market_points(
            issuer_id=_TOYOTA_ID,
            kind=MarketSeriesKind.DAILY_CLOSE,
            points=(
                _point(Source.JQUANTS, datetime(2025, 6, 1, tzinfo=UTC), Decimal("100")),
                _point(Source.YFINANCE, datetime(2025, 6, 1, tzinfo=UTC), Decimal("101")),
            ),
        )
        first.append_market_points(
            issuer_id=_SONY_ID,
            kind=MarketSeriesKind.DAILY_CLOSE,
            points=(
                _point(Source.JQUANTS, datetime(2025, 6, 1, tzinfo=UTC), Decimal("200")),
                _point(Source.YFINANCE, datetime(2025, 6, 2, tzinfo=UTC), Decimal("201")),
            ),
        )
        for filing in filings:
            first.append_filing(filing=filing)
        for relation in relations:
            first.append_relation(relation=relation)

    with DuckDbEntityStore(path=path) as second:
        assert sorted(second.list_all_issuers(), key=lambda issuer: issuer.id) == sorted(
            (toyota, sony),
            key=lambda issuer: issuer.id,
        )
        assert sorted(second.list_all_filings(), key=lambda filing: filing.doc_id) == list(filings)
        assert sorted(
            second.list_all_relations(),
            key=lambda relation: (relation.from_id, relation.to_id, relation.kind.value),
        ) == sorted(
            relations,
            key=lambda relation: (relation.from_id, relation.to_id, relation.kind.value),
        )


def _toyota() -> Issuer:
    return Issuer(
        id=_TOYOTA_ID,
        lei="5493006Z4DXP3JNCAY09",
        jcn="1180301018771",
        display_name="Toyota Motor Corporation",
        identifiers=(
            Identifier(kind=IdentifierKind.JQUANTS_CODE, value="72030"),
            Identifier(kind=IdentifierKind.EDINET_CODE, value="E02144"),
            Identifier(kind=IdentifierKind.YFINANCE_TICKER, value="7203.T"),
            Identifier(kind=IdentifierKind.JCN, value="1180301018771"),
            Identifier(kind=IdentifierKind.LEI, value="5493006Z4DXP3JNCAY09"),
        ),
    )


def _store_with_toyota(store: DuckDbEntityStore) -> DuckDbEntityStore:
    store.upsert_issuer(issuer=_toyota())
    return store


def _issuer(issuer_id: str, name: str) -> Issuer:
    return Issuer(id=issuer_id, lei=None, jcn=None, display_name=name, identifiers=())


def _provenance(source: Source) -> Provenance:
    return Provenance(
        source=source,
        fetched_at=datetime(2025, 6, 1, tzinfo=UTC),
        payload_hash="0" * 64,
    )


def _point(source: Source, timestamp: datetime, value: Decimal) -> MarketPoint:
    return MarketPoint(timestamp=timestamp, value=value, provenance=_provenance(source))


def _daily_points(
    *,
    source: Source,
    start: datetime,
    count: int,
    base: Decimal,
) -> tuple[MarketPoint, ...]:
    return tuple(
        _point(source, start + timedelta(days=offset), base + Decimal(offset))
        for offset in range(count)
    )


def _filing(
    *,
    doc_id: str,
    submitted_at: datetime,
    parent_doc_id: str | None = None,
) -> Filing:
    return Filing(
        issuer_id=_TOYOTA_ID,
        doc_id=doc_id,
        doc_type_code="120",
        submitted_at=submitted_at,
        parent_doc_id=parent_doc_id,
        provenance=_provenance(Source.EDINET),
    )


def _store_with_relation_graph(store: DuckDbEntityStore) -> DuckDbEntityStore:
    for issuer in (_issuer(_A_ID, "A"), _issuer(_B_ID, "B"), _issuer(_C_ID, "C")):
        store.upsert_issuer(issuer=issuer)
    store.append_relation(
        relation=Relation(
            from_id=_A_ID,
            to_id=_B_ID,
            kind=RelationKind.SUBSIDIARY_OF,
            valid_from=datetime(2020, 4, 1, tzinfo=UTC),
            valid_to=datetime(2024, 4, 1, tzinfo=UTC),
            provenance=_provenance(Source.EDINET),
        )
    )
    store.append_relation(
        relation=Relation(
            from_id=_A_ID,
            to_id=_C_ID,
            kind=RelationKind.SUBSIDIARY_OF,
            valid_from=datetime(2024, 4, 1, tzinfo=UTC),
            valid_to=None,
            provenance=_provenance(Source.EDINET),
        )
    )
    return store
