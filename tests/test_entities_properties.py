"""Property tests for caqrs.entities."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from caqrs.entities import (
    Filing,
    Identifier,
    IdentifierKind,
    InMemoryEntityStore,
    Issuer,
    MarketPoint,
    MarketSeriesKind,
    Provenance,
    Relation,
    RelationKind,
    Source,
)


@st.composite
def _identifier_set(draw: st.DrawFn) -> tuple[Identifier, ...]:
    raw_values = draw(
        st.lists(
            st.text(
                alphabet=st.characters(min_codepoint=48, max_codepoint=90),
                min_size=1,
                max_size=12,
            ),
            min_size=1,
            max_size=5,
            unique=True,
        )
    )
    kinds = [
        IdentifierKind.JQUANTS_CODE,
        IdentifierKind.SEC_CODE,
        IdentifierKind.YFINANCE_TICKER,
        IdentifierKind.EDINET_CODE,
        IdentifierKind.POLYMARKET_TOKEN,
    ]
    return tuple(
        Identifier(kind=kinds[index], value=value) for index, value in enumerate(raw_values)
    )


@given(offsets=st.lists(st.integers(min_value=0, max_value=365), min_size=1, unique=True))
def test_market_series_points_are_time_sorted(offsets: list[int]) -> None:
    store = InMemoryEntityStore()
    issuer = _issuer(
        "I0000000000000001", (Identifier(kind=IdentifierKind.JQUANTS_CODE, value="1"),)
    )
    store.upsert_issuer(issuer=issuer)
    base = datetime(2025, 1, 1, tzinfo=UTC)
    points = tuple(
        MarketPoint(
            timestamp=base + timedelta(days=offset),
            value=Decimal(offset),
            provenance=_provenance(Source.JQUANTS),
        )
        for offset in reversed(offsets)
    )
    store.append_market_points(
        issuer_id=issuer.id,
        kind=MarketSeriesKind.DAILY_CLOSE,
        points=points,
    )

    series = store.get_market_series(
        issuer_id=issuer.id,
        kind=MarketSeriesKind.DAILY_CLOSE,
        range_=(base, base + timedelta(days=365)),
        source_priority=(Source.JQUANTS,),
    )

    sorted_once = tuple(sorted(series.points, key=lambda point: point.timestamp))
    sorted_twice = tuple(sorted(sorted_once, key=lambda point: point.timestamp))
    assert sorted_once == series.points
    assert sorted_once == sorted_twice


@given(identifiers=_identifier_set())
def test_lookup_is_deterministic_for_any_issuer_identifier(
    identifiers: tuple[Identifier, ...],
) -> None:
    store = InMemoryEntityStore()
    issuer = _issuer("I0000000000000002", identifiers)
    store.upsert_issuer(issuer=issuer)

    for identifier in identifiers:
        assert store.lookup_issuer(kind=identifier.kind, value=identifier.value) == issuer


@given(day=st.integers(min_value=1, max_value=28), code=st.sampled_from(["120", "140", "160"]))
def test_filing_written_then_read_back_is_equal(day: int, code: str) -> None:
    store = InMemoryEntityStore()
    issuer = _issuer(
        "I0000000000000003", (Identifier(kind=IdentifierKind.EDINET_CODE, value="E1"),)
    )
    store.upsert_issuer(issuer=issuer)
    submitted_at = datetime(2025, 6, day, tzinfo=UTC)
    filing = Filing(
        issuer_id=issuer.id,
        doc_id=f"S{day:07d}",
        doc_type_code=code,
        submitted_at=submitted_at,
        parent_doc_id=None,
        provenance=_provenance(Source.EDINET),
    )

    store.append_filing(filing=filing)

    assert store.filings_for(
        issuer_id=issuer.id,
        range_=(submitted_at - timedelta(days=1), submitted_at + timedelta(days=1)),
        doc_type_codes=(code,),
    ) == (filing,)


@given(
    start_day=st.integers(min_value=1, max_value=20), duration=st.integers(min_value=1, max_value=8)
)
def test_relation_half_open_interval(start_day: int, duration: int) -> None:
    store = InMemoryEntityStore()
    source = _issuer("I0000000000000004", ())
    target = _issuer("I0000000000000005", ())
    store.upsert_issuer(issuer=source)
    store.upsert_issuer(issuer=target)
    valid_from = datetime(2025, 6, start_day, tzinfo=UTC)
    valid_to = valid_from + timedelta(days=duration)
    relation = Relation(
        from_id=source.id,
        to_id=target.id,
        kind=RelationKind.SUBSIDIARY_OF,
        valid_from=valid_from,
        valid_to=valid_to,
        provenance=_provenance(Source.EDINET),
    )
    store.append_relation(relation=relation)

    assert store.relations_for(issuer_id=source.id, at=valid_from) == (relation,)
    assert store.relations_for(issuer_id=source.id, at=valid_to) == ()


@given(
    valid_from=st.datetimes(
        min_value=datetime(2020, 1, 2),
        max_value=datetime(2026, 12, 1),
        timezones=st.just(UTC),
    ),
    duration=st.timedeltas(
        min_value=timedelta(days=2),
        max_value=timedelta(days=30),
    ),
    sampled_at=st.datetimes(
        min_value=datetime(2020, 1, 1),
        max_value=datetime(2027, 12, 31),
        timezones=st.just(UTC),
    ),
)
def test_relations_for_returns_relation_iff_at_is_inside_half_open_interval(
    valid_from: datetime,
    duration: timedelta,
    sampled_at: datetime,
) -> None:
    store = InMemoryEntityStore()
    source = _issuer("I0000000000000008", ())
    target = _issuer("I0000000000000009", ())
    store.upsert_issuer(issuer=source)
    store.upsert_issuer(issuer=target)
    valid_to = valid_from + duration
    relation = Relation(
        from_id=source.id,
        to_id=target.id,
        kind=RelationKind.SUBSIDIARY_OF,
        valid_from=valid_from,
        valid_to=valid_to,
        provenance=_provenance(Source.EDINET),
    )
    store.append_relation(relation=relation)

    sample_points = (
        valid_from - timedelta(microseconds=1),
        valid_from,
        valid_from + (duration / 2),
        valid_to,
        valid_to + timedelta(microseconds=1),
        sampled_at,
    )
    for at in sample_points:
        expected = (relation,) if valid_from <= at < valid_to else ()
        assert store.relations_for(issuer_id=source.id, at=at) == expected


@given(seed=st.integers(min_value=1, max_value=9999))
def test_merge_issuers_unions_disjoint_identifiers_idempotently(seed: int) -> None:
    store = InMemoryEntityStore()
    keep = _issuer(
        "I0000000000000006",
        (Identifier(kind=IdentifierKind.JQUANTS_CODE, value=f"{seed}A"),),
    )
    drop = _issuer(
        "I0000000000000007",
        (Identifier(kind=IdentifierKind.YFINANCE_TICKER, value=f"{seed}.T"),),
    )
    store.upsert_issuer(issuer=keep)
    store.upsert_issuer(issuer=drop)

    merged = store.merge_issuers(keep=keep.id, drop=drop.id)
    repeated = store.merge_issuers(keep=keep.id, drop=drop.id)

    assert set(merged.identifiers) == {*keep.identifiers, *drop.identifiers}
    assert repeated == merged
    assert store.lookup_issuer(kind=IdentifierKind.YFINANCE_TICKER, value=f"{seed}.T") == merged


def _issuer(issuer_id: str, identifiers: tuple[Identifier, ...]) -> Issuer:
    return Issuer(
        id=issuer_id,
        lei=None,
        jcn=None,
        display_name=f"Issuer {issuer_id}",
        identifiers=identifiers,
    )


def _provenance(source: Source) -> Provenance:
    return Provenance(
        source=source,
        fetched_at=datetime(2025, 6, 1, tzinfo=UTC),
        payload_hash="0" * 64,
    )
