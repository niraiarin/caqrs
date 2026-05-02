"""Performance smoke gates for the entity stores (Task #89).

Two NFRs are guarded here:

* **NFR-PERF-1** (issuer lookup latency): single-key issuer lookup against
  a populated store at 10k issuers must stay under 1ms (in-memory) and
  under 5ms (DuckDB) at the median.
* **NFR-PERF-3** (DuckDB cold-open round-trip): opening an existing
  DuckDB file and reading all issuers (after seeding 100 issuers x 1000
  market_points = 100k points) must stay under 200ms at the median.

The tests use ``pytest-benchmark``; ``benchmark.stats["median"]`` and
``benchmark.stats["mean"]`` are populated after ``benchmark(callable)``
returns. We assert on **median** (robust to outliers — the runner can be
preempted by other CI jobs).

These tests are deselected from the default suite via the ``perf``
marker (see ``pyproject.toml``'s ``addopts``); run them with
``just perf``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pytest_benchmark.fixture import BenchmarkFixture

from caqrs.entities import (
    Identifier,
    IdentifierKind,
    Issuer,
    MarketPoint,
    MarketSeriesKind,
    Provenance,
    Source,
)
from caqrs.entities.duckdb import DuckDbEntityStore
from caqrs.entities.in_memory import InMemoryEntityStore
from caqrs.entities.protocol import EntityStore

pytestmark = pytest.mark.perf

# Issuer-lookup scale (NFR-PERF-1).
_ISSUER_COUNT = 10_000
# Pick a target deep enough into the keyspace that lookup is not the first
# entry in any per-shard hash bucket; offset chosen arbitrarily within range.
_LOOKUP_INDEX = 5_000

# DuckDB cold-open scale (NFR-PERF-3): 100 issuers x 1000 points = 100k points.
_COLD_ISSUER_COUNT = 100
_COLD_POINTS_PER_ISSUER = 1_000


def _make_issuer(index: int) -> Issuer:
    """Build a deterministic Issuer with one JQUANTS_CODE identifier."""
    issuer_id = f"I{index:016x}"
    identifier = Identifier(
        kind=IdentifierKind.JQUANTS_CODE,
        value=f"{10_000 + index}",
    )
    return Issuer(
        id=issuer_id,
        lei=None,
        jcn=None,
        display_name=f"Issuer {index}",
        identifiers=(identifier,),
    )


def _seed_issuers(store: EntityStore, count: int) -> list[str]:
    """Populate ``count`` issuers; return their ids in insertion order."""
    issuer_ids: list[str] = []
    for i in range(count):
        issuer = _make_issuer(i)
        store.upsert_issuer(issuer=issuer)
        issuer_ids.append(issuer.id)
    return issuer_ids


def _build_provenance(fetched_at: datetime) -> Provenance:
    return Provenance(
        source=Source.JQUANTS,
        fetched_at=fetched_at,
        payload_hash="0" * 64,
    )


def _make_points(count: int, *, base_value: int) -> tuple[MarketPoint, ...]:
    """Build a deterministic run of daily MarketPoints starting 2024-01-01."""
    base_ts = datetime(2024, 1, 1, tzinfo=UTC)
    fetched = datetime(2024, 1, 1, tzinfo=UTC)
    provenance = _build_provenance(fetched)
    return tuple(
        MarketPoint(
            timestamp=base_ts + timedelta(days=d),
            value=Decimal(base_value + d),
            provenance=provenance,
        )
        for d in range(count)
    )


@pytest.mark.traces("NFR-PERF-1")
def test_in_memory_issuer_lookup_p95_under_1ms_at_10k(
    benchmark: BenchmarkFixture,
) -> None:
    """In-memory issuer lookup median latency must stay under 1ms at 10k issuers."""
    store = InMemoryEntityStore()
    _seed_issuers(store, _ISSUER_COUNT)
    target_value = f"{10_000 + _LOOKUP_INDEX}"

    def lookup() -> Issuer | None:
        return store.lookup_issuer(
            kind=IdentifierKind.JQUANTS_CODE,
            value=target_value,
        )

    result = benchmark(lookup)

    assert result is not None
    assert result.id == f"I{_LOOKUP_INDEX:016x}"
    median = benchmark.stats["median"]
    assert median < 0.001, (
        f"NFR-PERF-1 (in-memory) breach: median lookup = {median * 1000:.3f}ms (threshold 1ms)"
    )


@pytest.mark.traces("NFR-PERF-1")
def test_duckdb_issuer_lookup_p95_under_5ms_at_10k(
    benchmark: BenchmarkFixture,
    tmp_path: Path,
) -> None:
    """DuckDB issuer lookup median latency must stay under 5ms at 10k issuers."""
    db_path = tmp_path / "perf.duckdb"
    with DuckDbEntityStore(path=db_path) as store:
        _seed_issuers(store, _ISSUER_COUNT)
        target_value = f"{10_000 + _LOOKUP_INDEX}"

        def lookup() -> Issuer | None:
            return store.lookup_issuer(
                kind=IdentifierKind.JQUANTS_CODE,
                value=target_value,
            )

        result = benchmark(lookup)

        assert result is not None
        assert result.id == f"I{_LOOKUP_INDEX:016x}"
        median = benchmark.stats["median"]
        assert median < 0.005, (
            f"NFR-PERF-1 (DuckDB) breach: median lookup = {median * 1000:.3f}ms (threshold 5ms)"
        )


@pytest.mark.traces("NFR-PERF-3")
def test_duckdb_cold_open_round_trip_under_200ms(
    benchmark: BenchmarkFixture,
    tmp_path: Path,
) -> None:
    """Re-open a 100k-point DuckDB file and read all issuers under 200ms (median).

    Cold-state setup: write a DuckDB file with 100 issuers x 1000 market_points
    each (= 100k points). The benchmark closes that connection and times a
    fresh ``DuckDbEntityStore(path=...)`` open + ``list_all_issuers()`` round
    trip. This is the realistic agent-startup path: a long-lived database
    file, opened by a fresh process each cycle.
    """
    db_path = tmp_path / "cold_open.duckdb"

    # Cold-state setup: build the file, then close.
    with DuckDbEntityStore(path=db_path) as store:
        issuer_ids = _seed_issuers(store, _COLD_ISSUER_COUNT)
        for i, issuer_id in enumerate(issuer_ids):
            points = _make_points(_COLD_POINTS_PER_ISSUER, base_value=100 + i)
            store.append_market_points(
                issuer_id=issuer_id,
                kind=MarketSeriesKind.DAILY_CLOSE,
                points=points,
            )

    def cold_round_trip() -> tuple[Issuer, ...]:
        with DuckDbEntityStore(path=db_path) as store:
            return store.list_all_issuers()

    result = benchmark(cold_round_trip)

    assert len(result) == _COLD_ISSUER_COUNT
    median = benchmark.stats["median"]
    assert median < 0.200, (
        f"NFR-PERF-3 breach: median cold-open round-trip = {median * 1000:.1f}ms (threshold 200ms)"
    )
