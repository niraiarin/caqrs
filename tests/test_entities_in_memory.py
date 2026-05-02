"""Tests for InMemoryEntityStore."""

# T11 (round-trip from a fresh process via DuckDB file) is deferred to Phase E2.
# This file covers T1-T10 and T12. P1-P5 live in tests/test_entities_properties.py.

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic_core import to_json

from caqrs.entities import (
    ConflictRecord,
    Filing,
    Identifier,
    IdentifierConflictError,
    IdentifierKind,
    InMemoryEntityStore,
    Issuer,
    MarketPoint,
    MarketSeriesKind,
    Provenance,
    Relation,
    RelationKind,
    Source,
    UnknownIssuerError,
)

_TOYOTA_ID = "I0000000000000001"
_A_ID = "I000000000000000a"
_B_ID = "I000000000000000b"
_C_ID = "I000000000000000c"


def test_empty_store_lookup_returns_none() -> None:
    store = InMemoryEntityStore()

    assert store.lookup_issuer(kind=IdentifierKind.JQUANTS_CODE, value="72030") is None


def test_lookup_by_each_toyota_identifier_returns_same_issuer() -> None:
    store = InMemoryEntityStore()
    issuer = _toyota()
    store.upsert_issuer(issuer=issuer)

    assert store.lookup_issuer(kind=IdentifierKind.JQUANTS_CODE, value="72030") == issuer
    assert store.lookup_issuer(kind=IdentifierKind.EDINET_CODE, value="E02144") == issuer
    assert store.lookup_issuer(kind=IdentifierKind.YFINANCE_TICKER, value="7203.T") == issuer


def test_lookup_by_jcn_then_lei_returns_equal_issuer() -> None:
    store = InMemoryEntityStore()
    issuer = _toyota()
    store.upsert_issuer(issuer=issuer)

    via_jcn = store.lookup_issuer(kind=IdentifierKind.JCN, value="1180301018771")
    via_lei = store.lookup_issuer(kind=IdentifierKind.LEI, value="5493006Z4DXP3JNCAY09")

    assert via_jcn == via_lei == issuer


def test_upsert_rejects_identifier_registered_to_different_issuer() -> None:
    store = InMemoryEntityStore()
    store.upsert_issuer(issuer=_toyota())
    other = Issuer(
        id="I0000000000000002",
        lei=None,
        jcn=None,
        display_name="Other",
        identifiers=(Identifier(kind=IdentifierKind.JQUANTS_CODE, value="72030"),),
    )

    with pytest.raises(IdentifierConflictError):
        store.upsert_issuer(issuer=other)


def test_identifier_conflict_error_exposes_payload_fields() -> None:
    store = InMemoryEntityStore()
    toyota = _toyota()
    store.upsert_issuer(issuer=toyota)
    other_issuer = Issuer(
        id="I0000000000000002",
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


def test_market_series_uses_jquants_when_priority_covers_whole_range() -> None:
    store = _store_with_toyota()
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


def test_market_series_falls_back_to_yfinance_for_uncovered_days() -> None:
    store = _store_with_toyota()
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


def test_t6_conflict_log_records_disagreed_points() -> None:
    store = _store_with_toyota()
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
    assert series.conflict_log[0].chosen == higher_priority
    assert series.conflict_log[0].discarded == (lower_priority,)


def test_append_market_points_rejects_unknown_issuer() -> None:
    store = InMemoryEntityStore()

    with pytest.raises(UnknownIssuerError):
        store.append_market_points(
            issuer_id=_TOYOTA_ID,
            kind=MarketSeriesKind.DAILY_CLOSE,
            points=(_point(Source.JQUANTS, datetime(2025, 6, 1, tzinfo=UTC), Decimal("1")),),
        )


def test_append_filing_with_unknown_issuer_raises() -> None:
    store = InMemoryEntityStore()
    filing = _filing(doc_id="S1000001", submitted_at=datetime(2025, 6, 1, 1, tzinfo=UTC))

    with pytest.raises(UnknownIssuerError):
        store.append_filing(filing=filing)


def test_append_filing_then_filings_for_returns_filing() -> None:
    store = _store_with_toyota()
    filing = _filing(doc_id="S1000001", submitted_at=datetime(2025, 6, 1, 1, tzinfo=UTC))
    store.append_filing(filing=filing)

    assert store.filings_for(
        issuer_id=_TOYOTA_ID,
        range_=(datetime(2025, 6, 1, tzinfo=UTC), datetime(2025, 6, 2, tzinfo=UTC)),
        doc_type_codes=("120",),
    ) == (filing,)


def test_corrective_filing_is_returned_in_submit_time_order() -> None:
    store = _store_with_toyota()
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


def test_subsidiaries_of_returns_relation_active_on_last_effective_day() -> None:
    store = _store_with_relation_graph()

    assert store.subsidiaries_of(
        issuer_id=_B_ID,
        at=datetime(2024, 3, 31, tzinfo=UTC),
    ) == (store.get_issuer(issuer_id=_A_ID),)


def test_subsidiaries_of_excludes_relation_on_exclusive_end_date() -> None:
    store = _store_with_relation_graph()

    assert (
        store.subsidiaries_of(
            issuer_id=_B_ID,
            at=datetime(2024, 4, 1, tzinfo=UTC),
        )
        == ()
    )


def test_append_relation_with_unknown_issuer_raises() -> None:
    store = InMemoryEntityStore()
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


def test_market_point_provenance_preserves_jquants_payload_hash() -> None:
    store = _store_with_toyota()
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


def _store_with_toyota() -> InMemoryEntityStore:
    store = InMemoryEntityStore()
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


def _store_with_relation_graph() -> InMemoryEntityStore:
    store = InMemoryEntityStore()
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
