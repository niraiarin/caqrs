"""Phase E3 reconciliation seed loaders — failing test list (TDD step 1).

Per ADR-0006, this module ships first as a list of xfail(strict=True)
tests. The implementation in step 2 flips them to passing (except T22,
which stays xfail — GLEIF deferred to Phase E5 per spec section F).

REQ-IDs covered:
  ENT-RECON-T20..T30 (11 example tests)
  ENT-RECON-P6..P9   (4 property tests)

Spec: docs/research/data-integration/03-phase-e3-reconciliation.md
ADR-0006: docs/decisions/0006-two-step-tdd-dispatch.md

Client mocking strategy
-----------------------
JQuantsClient and EdinetDbClient are concrete httpx-backed classes; we
mock at the HTTP layer with respx and let the real client objects run.
This matches tests/test_jquants_client.py and tests/test_edinetdb_client.py.

Property tests use Hypothesis to drive small synthetic record sets
through the loaders (mocking the same HTTP layer per generated example).
"""

from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Iterable
from typing import Any

import httpx
import pytest
import respx
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic_core import to_json

from caqrs.data.edinetdb.client import EdinetDbClient
from caqrs.data.jquants.client import JQuantsClient
from caqrs.entities import (
    Identifier,
    IdentifierKind,
    InMemoryEntityStore,
    Issuer,
    Source,
)
from caqrs.entities import reconciliation as recon_module
from caqrs.entities.reconciliation import (
    ReconcilerResult,
    reconcile_from_edinetdb_companies,
    reconcile_from_jquants_master,
)

_JQ_BASE = "https://api.jquants.com/v2"
_EDB_BASE = "https://edinetdb.jp/v1"
_JQ_KEY = "test-jq-key"
_EDB_KEY = "edb_" + "0" * 32

_HYPOTHESIS_SETTINGS = settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

# === Fixture builders ===


def _jq_row(*, code: str, company_name: str, date: str = "2026-05-02") -> dict[str, Any]:
    """Wire-format J-Quants GET /v2/equities/master row."""
    return {"Date": date, "Code": code, "CoName": company_name}


def _edb_row(
    *,
    edinet_code: str,
    name: str,
    sec_code: str | None = None,
    jcn: str | None = None,
    industry: str = "サービス業",
    accounting_standard: str = "JP",
) -> dict[str, Any]:
    """Wire-format EDINET DB GET /v1/companies row."""
    row: dict[str, Any] = {
        "edinet_code": edinet_code,
        "name": name,
        "name_ja": name,
        "industry": industry,
        "accounting_standard": accounting_standard,
    }
    if sec_code is not None:
        row["sec_code"] = sec_code
    if jcn is not None:
        row["jcn"] = jcn
    return row


def _edb_page(
    *,
    rows: list[dict[str, Any]],
    page: int,
    per_page: int,
    total: int | None = None,
) -> dict[str, Any]:
    pagination: dict[str, Any] = {"page": page, "per_page": per_page}
    if total is not None:
        pagination["total"] = total
    return {"data": rows, "meta": {"pagination": pagination}}


def _async_run(coro: Any) -> Any:
    """Run an async coroutine inside a Hypothesis sync test."""
    return asyncio.get_event_loop().run_until_complete(coro)


# === T20 — J-Quants happy path ===


@pytest.mark.traces("ENT-RECON-T20", "ENT-RECON-A1", "ENT-RECON-A6")
@pytest.mark.xfail(strict=True, reason="impl pending — ADR-0006 step 1 (Task #85)")
@respx.mock
async def test_t20_jquants_happy_path_three_rows() -> None:
    """T20: A fresh InMemoryEntityStore + a J-Quants stub yielding three
    rows (Toyota / Sony / NTT) returns ReconcilerResult(upserted=3,
    skipped=0, conflicts=(), dry_run=False). The store contains three
    Issuers; each has both (JQUANTS_CODE, <5-digit>) and (SEC_CODE,
    <4-digit>) identifiers."""
    respx.get(f"{_JQ_BASE}/equities/master").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    _jq_row(code="72030", company_name="Toyota Motor Corporation"),
                    _jq_row(code="67580", company_name="Sony Group Corporation"),
                    _jq_row(code="94320", company_name="Nippon Telegraph and Telephone"),
                ],
            },
        ),
    )

    store = InMemoryEntityStore()
    async with JQuantsClient(api_key=_JQ_KEY) as client:
        result = await reconcile_from_jquants_master(client=client, store=store)

    assert result == ReconcilerResult(upserted=3, skipped=0, conflicts=(), dry_run=False)

    toyota = store.lookup_issuer(kind=IdentifierKind.JQUANTS_CODE, value="72030")
    sony = store.lookup_issuer(kind=IdentifierKind.JQUANTS_CODE, value="67580")
    ntt = store.lookup_issuer(kind=IdentifierKind.JQUANTS_CODE, value="94320")
    assert toyota is not None
    assert sony is not None
    assert ntt is not None
    assert toyota.display_name == "Toyota Motor Corporation"
    assert {ident.kind for ident in toyota.identifiers} == {
        IdentifierKind.JQUANTS_CODE,
        IdentifierKind.SEC_CODE,
    }
    # The 4-digit SEC_CODE is the 5-digit code with the trailing check digit stripped.
    assert store.lookup_issuer(kind=IdentifierKind.SEC_CODE, value="7203") == toyota
    assert store.lookup_issuer(kind=IdentifierKind.SEC_CODE, value="6758") == sony
    assert store.lookup_issuer(kind=IdentifierKind.SEC_CODE, value="9432") == ntt


# === T21 — EDINET DB happy path with pagination (1200 rows / 3 pages of 500) ===


@pytest.mark.traces("ENT-RECON-T21", "ENT-RECON-A1", "ENT-RECON-A6")
@pytest.mark.xfail(strict=True, reason="impl pending — ADR-0006 step 1 (Task #85)")
@respx.mock
async def test_t21_edinetdb_happy_path_paginates_1200_rows() -> None:
    """T21: An EDINET DB stub paginating 1200 rows over 3 pages of 500
    returns ReconcilerResult(upserted=1200, skipped=0, conflicts=(),
    dry_run=False). Every row's Issuer carries an (EDINET_CODE, ...)
    identifier; rows whose source record had a JCN also carry
    (JCN, ...)."""
    page1 = [_edb_row(edinet_code=f"E{i:05d}", name=f"Company {i}") for i in range(500)]
    page2 = [
        _edb_row(
            edinet_code=f"E{i:05d}",
            name=f"Company {i}",
            jcn=f"{1000000000000 + i}",  # 13-digit
        )
        for i in range(500, 1000)
    ]
    page3 = [_edb_row(edinet_code=f"E{i:05d}", name=f"Company {i}") for i in range(1000, 1200)]

    pages_by_index = {"1": page1, "2": page2, "3": page3}

    def _handler(request: httpx.Request) -> httpx.Response:
        page_str = request.url.params.get("page", "1")
        rows = pages_by_index[page_str]
        return httpx.Response(
            200,
            json=_edb_page(rows=rows, page=int(page_str), per_page=500, total=1200),
        )

    respx.get(f"{_EDB_BASE}/companies").mock(side_effect=_handler)

    store = InMemoryEntityStore()
    async with EdinetDbClient(api_key=_EDB_KEY) as client:
        result = await reconcile_from_edinetdb_companies(client=client, store=store)

    assert result == ReconcilerResult(upserted=1200, skipped=0, conflicts=(), dry_run=False)
    # Every row produced an EDINET_CODE; JCN-bearing rows produced JCN identifiers.
    assert store.lookup_issuer(kind=IdentifierKind.EDINET_CODE, value="E00000") is not None
    assert store.lookup_issuer(kind=IdentifierKind.EDINET_CODE, value="E01199") is not None
    assert store.lookup_issuer(kind=IdentifierKind.JCN, value="1000000000500") is not None
    # Row from page 1 had no JCN: no JCN identifier should sneak in.
    no_jcn = store.lookup_issuer(kind=IdentifierKind.EDINET_CODE, value="E00000")
    assert no_jcn is not None
    assert IdentifierKind.JCN not in {ident.kind for ident in no_jcn.identifiers}


# === T22 — GLEIF deferred (Phase E5) ===


@pytest.mark.traces("ENT-RECON-T22", "ENT-RECON-A1")
@pytest.mark.xfail(strict=True, reason="GLEIF LEI loader deferred to Phase E5 per spec section F")
def test_t22_gleif_lei_loader_placeholder() -> None:
    """T22 (deferred to Phase E5): Documents the contract the GLEIF LEI
    loader will satisfy. Stays xfail through both Phase E3 steps; Phase
    E5 introduces reconcile_from_gleif_lei and flips this marker when
    the bulk-file streamer + LEI-emitting Issuer construction lands.

    The assertion below intentionally requires the future symbol to be
    importable from caqrs.entities.reconciliation; until Phase E5 it is
    not, and the xfail is genuine."""
    assert hasattr(recon_module, "reconcile_from_gleif_lei"), (
        "Phase E5: GLEIF LEI loader expected at "
        "caqrs.entities.reconciliation.reconcile_from_gleif_lei"
    )


# === T23 — Idempotency ===


@pytest.mark.traces("ENT-RECON-T23", "ENT-RECON-A2")
@pytest.mark.xfail(strict=True, reason="impl pending — ADR-0006 step 1 (Task #85)")
@respx.mock
async def test_t23_jquants_idempotent_rerun_reports_skipped_count() -> None:
    """T23: A store already populated by one J-Quants run; a second call
    with identical input returns ReconcilerResult(upserted=0, skipped=N,
    conflicts=(), dry_run=False). The store snapshot before and after
    the second call is identical issuer-for-issuer."""
    rows = [
        _jq_row(code="72030", company_name="Toyota Motor Corporation"),
        _jq_row(code="67580", company_name="Sony Group Corporation"),
    ]
    respx.get(f"{_JQ_BASE}/equities/master").mock(
        return_value=httpx.Response(200, json={"data": rows}),
    )

    store = InMemoryEntityStore()
    async with JQuantsClient(api_key=_JQ_KEY) as client:
        first = await reconcile_from_jquants_master(client=client, store=store)
        snapshot_before = _snapshot_issuers(store)
        second = await reconcile_from_jquants_master(client=client, store=store)

    assert first.upserted == 2
    assert second.upserted == 0
    assert second.skipped == 2
    assert second.conflicts == ()
    assert second.dry_run is False
    assert _snapshot_issuers(store) == snapshot_before


# === T24 — Provenance population ===


@pytest.mark.traces("ENT-RECON-T24", "ENT-RECON-A3")
@pytest.mark.xfail(strict=True, reason="impl pending — ADR-0006 step 1 (Task #85)")
@respx.mock
async def test_t24_jquants_provenance_carries_source_fetched_at_payload_hash() -> None:
    """T24: A J-Quants stub with a single row; the per-record provenance
    written by the loader has source == Source.JQUANTS, fetched_at is a
    tz-aware UTC datetime, and payload_hash ==
    sha256(canonical_json(raw_row)).hexdigest() (i.e., the loader hashes
    the original row, not a placeholder).

    The implementor in step 2 chooses where Provenance is exposed (on
    result.provenance, on a sentinel attached to the Issuer, etc.); this
    test asserts the *values* via _extract_provenance_for_jquants which
    recognises both shapes."""
    raw_row = _jq_row(code="72030", company_name="Toyota Motor Corporation")
    respx.get(f"{_JQ_BASE}/equities/master").mock(
        return_value=httpx.Response(200, json={"data": [raw_row]}),
    )

    store = InMemoryEntityStore()
    async with JQuantsClient(api_key=_JQ_KEY) as client:
        result = await reconcile_from_jquants_master(client=client, store=store)

    assert result.upserted == 1
    provenance = _extract_provenance_for_jquants(result, store, code="72030")
    assert provenance is not None
    assert provenance.source == Source.JQUANTS
    assert provenance.fetched_at.tzinfo is not None
    expected_hash = hashlib.sha256(to_json(raw_row)).hexdigest()
    assert provenance.payload_hash == expected_hash


# === T25 — Conflict handling: collected, not raised ===


@pytest.mark.traces("ENT-RECON-T25", "ENT-RECON-A4")
@pytest.mark.xfail(strict=True, reason="impl pending — ADR-0006 step 1 (Task #85)")
@respx.mock
async def test_t25_jquants_identifier_conflict_collected_not_raised() -> None:
    """T25: Store pre-populated with Issuer X owning (JQUANTS_CODE,
    "72030"); J-Quants stub yields a row that would bind the same code
    to a different Issuer Y. IdentifierConflictError is caught, appended
    to result.conflicts (one structured message naming kind, value, and
    the existing issuer id), and the loop continues. Issuer X is
    unchanged; Y is not persisted."""
    existing = Issuer(
        id="I" + "a" * 16,
        lei=None,
        jcn=None,
        display_name="Existing Holder of 72030",
        identifiers=(Identifier(kind=IdentifierKind.JQUANTS_CODE, value="72030"),),
    )
    store = InMemoryEntityStore()
    store.upsert_issuer(issuer=existing)
    snapshot_before = _snapshot_issuers(store)

    respx.get(f"{_JQ_BASE}/equities/master").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [_jq_row(code="72030", company_name="Synthetic Y, not Toyota")],
            },
        ),
    )

    async with JQuantsClient(api_key=_JQ_KEY) as client:
        result = await reconcile_from_jquants_master(client=client, store=store)

    assert result.upserted == 0
    assert result.skipped == 1
    assert len(result.conflicts) == 1
    message = result.conflicts[0]
    # Structured message references the conflicting kind, the value,
    # and the existing issuer id (per spec section D format).
    assert "jquants_code" in message
    assert "72030" in message
    assert existing.id in message
    # Snapshot unchanged; Y was never persisted.
    assert _snapshot_issuers(store) == snapshot_before


# === T26 — Dry-run mode ===


@pytest.mark.traces("ENT-RECON-T26", "ENT-RECON-A5")
@pytest.mark.xfail(strict=True, reason="impl pending — ADR-0006 step 1 (Task #85)")
@respx.mock
async def test_t26_jquants_dry_run_does_not_mutate_store() -> None:
    """T26: A fresh store + the three-row J-Quants stub from T20 with
    dry_run=True. Returns ReconcilerResult(upserted=3, skipped=0,
    conflicts=(), dry_run=True). Store is byte-for-byte unchanged."""
    rows = [
        _jq_row(code="72030", company_name="Toyota"),
        _jq_row(code="67580", company_name="Sony"),
        _jq_row(code="94320", company_name="NTT"),
    ]
    respx.get(f"{_JQ_BASE}/equities/master").mock(
        return_value=httpx.Response(200, json={"data": rows}),
    )

    store = InMemoryEntityStore()
    async with JQuantsClient(api_key=_JQ_KEY) as client:
        result = await reconcile_from_jquants_master(client=client, store=store, dry_run=True)

    assert result == ReconcilerResult(upserted=3, skipped=0, conflicts=(), dry_run=True)
    # Store is byte-for-byte unchanged: lookup misses on every input id.
    assert store.lookup_issuer(kind=IdentifierKind.JQUANTS_CODE, value="72030") is None
    assert store.lookup_issuer(kind=IdentifierKind.JQUANTS_CODE, value="67580") is None
    assert store.lookup_issuer(kind=IdentifierKind.JQUANTS_CODE, value="94320") is None


# === T27 — Source-specific id taxonomy: J-Quants ===


@pytest.mark.traces("ENT-RECON-T27", "ENT-RECON-A6")
@pytest.mark.xfail(strict=True, reason="impl pending — ADR-0006 step 1 (Task #85)")
@respx.mock
async def test_t27_jquants_row_emits_exactly_jquants_code_and_sec_code() -> None:
    """T27: A J-Quants stub with a single row Code=72030; the persisted
    Issuer.identifiers contains exactly two entries:
    Identifier(JQUANTS_CODE, "72030") and Identifier(SEC_CODE, "7203").
    No EDINET_CODE / JCN / LEI is invented."""
    respx.get(f"{_JQ_BASE}/equities/master").mock(
        return_value=httpx.Response(
            200,
            json={"data": [_jq_row(code="72030", company_name="Toyota")]},
        ),
    )

    store = InMemoryEntityStore()
    async with JQuantsClient(api_key=_JQ_KEY) as client:
        await reconcile_from_jquants_master(client=client, store=store)

    issuer = store.lookup_issuer(kind=IdentifierKind.JQUANTS_CODE, value="72030")
    assert issuer is not None
    assert set(issuer.identifiers) == {
        Identifier(kind=IdentifierKind.JQUANTS_CODE, value="72030"),
        Identifier(kind=IdentifierKind.SEC_CODE, value="7203"),
    }


# === T28 — EDINET DB row with edinet+jcn+sec emits all three ===


@pytest.mark.traces("ENT-RECON-T28", "ENT-RECON-A6")
@pytest.mark.xfail(strict=True, reason="impl pending — ADR-0006 step 1 (Task #85)")
@respx.mock
async def test_t28_edinetdb_row_emits_edinet_jcn_and_sec() -> None:
    """T28: An EDINET DB row with edinet_code="E02144", jcn="1180301018771",
    sec_code="7203"; the persisted Issuer.identifiers contains all three:
    (EDINET_CODE, "E02144"), (JCN, "1180301018771"), (SEC_CODE, "7203")."""
    rows = [
        _edb_row(
            edinet_code="E02144",
            name="トヨタ自動車株式会社",
            sec_code="7203",
            jcn="1180301018771",
        ),
    ]
    respx.get(f"{_EDB_BASE}/companies").mock(
        return_value=httpx.Response(
            200,
            json=_edb_page(rows=rows, page=1, per_page=500, total=1),
        ),
    )

    store = InMemoryEntityStore()
    async with EdinetDbClient(api_key=_EDB_KEY) as client:
        await reconcile_from_edinetdb_companies(client=client, store=store)

    issuer = store.lookup_issuer(kind=IdentifierKind.EDINET_CODE, value="E02144")
    assert issuer is not None
    assert set(issuer.identifiers) == {
        Identifier(kind=IdentifierKind.EDINET_CODE, value="E02144"),
        Identifier(kind=IdentifierKind.JCN, value="1180301018771"),
        Identifier(kind=IdentifierKind.SEC_CODE, value="7203"),
    }


# === T29 — Partial-data EDINET DB record (edinet only) ===


@pytest.mark.traces("ENT-RECON-T29", "ENT-RECON-A6")
@pytest.mark.xfail(strict=True, reason="impl pending — ADR-0006 step 1 (Task #85)")
@respx.mock
async def test_t29_edinetdb_partial_record_emits_only_edinet_code() -> None:
    """T29: An EDINET DB row with only edinet_code="E12345" (jcn,
    sec_code both None); the persisted Issuer.identifiers contains
    exactly one entry: (EDINET_CODE, "E12345"). No empty-string JCN or
    empty-string SEC_CODE invented. upserted=1, skipped=0."""
    rows = [_edb_row(edinet_code="E12345", name="Some Trust Fund")]
    respx.get(f"{_EDB_BASE}/companies").mock(
        return_value=httpx.Response(
            200,
            json=_edb_page(rows=rows, page=1, per_page=500, total=1),
        ),
    )

    store = InMemoryEntityStore()
    async with EdinetDbClient(api_key=_EDB_KEY) as client:
        result = await reconcile_from_edinetdb_companies(client=client, store=store)

    assert result.upserted == 1
    assert result.skipped == 0
    issuer = store.lookup_issuer(kind=IdentifierKind.EDINET_CODE, value="E12345")
    assert issuer is not None
    assert set(issuer.identifiers) == {
        Identifier(kind=IdentifierKind.EDINET_CODE, value="E12345"),
    }


# === T30 — Batch boundary (paginated source) walks all pages ===


@pytest.mark.traces("ENT-RECON-T30")
@pytest.mark.xfail(strict=True, reason="impl pending — ADR-0006 step 1 (Task #85)")
@respx.mock
async def test_t30_edinetdb_walks_three_pages_of_500() -> None:
    """T30: An EDINET DB stub paginating 1500 rows over 3 pages of 500;
    result.upserted == 1500. The reconciler walks all three pages
    (pagination boundary correctness)."""

    pages = {
        "1": [_edb_row(edinet_code=f"E{i:05d}", name=f"C{i}") for i in range(0, 500)],
        "2": [_edb_row(edinet_code=f"E{i:05d}", name=f"C{i}") for i in range(500, 1000)],
        "3": [_edb_row(edinet_code=f"E{i:05d}", name=f"C{i}") for i in range(1000, 1500)],
    }

    def _handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page", "1")
        return httpx.Response(
            200,
            json=_edb_page(rows=pages[page], page=int(page), per_page=500, total=1500),
        )

    respx.get(f"{_EDB_BASE}/companies").mock(side_effect=_handler)

    store = InMemoryEntityStore()
    async with EdinetDbClient(api_key=_EDB_KEY) as client:
        result = await reconcile_from_edinetdb_companies(client=client, store=store)

    assert result.upserted == 1500
    # Spot-check one row from each page.
    assert store.lookup_issuer(kind=IdentifierKind.EDINET_CODE, value="E00000") is not None
    assert store.lookup_issuer(kind=IdentifierKind.EDINET_CODE, value="E00750") is not None
    assert store.lookup_issuer(kind=IdentifierKind.EDINET_CODE, value="E01499") is not None


# === Property tests P6..P9 ===


def _hypothesis_jq_codes() -> st.SearchStrategy[list[str]]:
    """Distinct 5-digit J-Quants codes."""
    return st.lists(
        st.integers(min_value=10000, max_value=99999).map(lambda i: f"{i:05d}"),
        min_size=1,
        max_size=10,
        unique=True,
    )


@pytest.mark.traces("ENT-RECON-P6", "ENT-RECON-A2")
@pytest.mark.xfail(strict=True, reason="impl pending — ADR-0006 step 1 (Task #85)")
@_HYPOTHESIS_SETTINGS
@given(codes=_hypothesis_jq_codes())
def test_p6_jquants_idempotency_under_repeated_runs(codes: list[str]) -> None:
    """P6: For any list R of distinct J-Quants codes (size <= 10),
    running the loader twice on a store yields the same store state as
    running it once (idempotency under repeated runs)."""

    rows = [_jq_row(code=code, company_name=f"Issuer {code}") for code in codes]

    async def _scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        with respx.mock(base_url=_JQ_BASE):
            respx.get(f"{_JQ_BASE}/equities/master").mock(
                return_value=httpx.Response(200, json={"data": rows}),
            )

            once = InMemoryEntityStore()
            twice = InMemoryEntityStore()
            async with JQuantsClient(api_key=_JQ_KEY) as client:
                await reconcile_from_jquants_master(client=client, store=once)
                await reconcile_from_jquants_master(client=client, store=twice)
                await reconcile_from_jquants_master(client=client, store=twice)
            return _snapshot_issuers(once), _snapshot_issuers(twice)

    once_state, twice_state = _async_run(_scenario())
    assert once_state == twice_state


@pytest.mark.traces("ENT-RECON-P7")
@pytest.mark.xfail(strict=True, reason="impl pending — ADR-0006 step 1 (Task #85)")
@_HYPOTHESIS_SETTINGS
@given(codes=_hypothesis_jq_codes(), seed=st.integers(min_value=0, max_value=2**31 - 1))
def test_p7_jquants_source_order_independence(codes: list[str], seed: int) -> None:
    """P7: For any list R and any permutation pi(R), the resulting store
    state (compared as a set of (display_name, identifier-set) keys) is
    equal."""
    rng = random.Random(seed)
    permuted = list(codes)
    rng.shuffle(permuted)
    if permuted == codes and len(codes) > 1:
        permuted = list(reversed(codes))

    async def _scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        rows_a = [_jq_row(code=code, company_name=f"Issuer {code}") for code in codes]
        rows_b = [_jq_row(code=code, company_name=f"Issuer {code}") for code in permuted]

        with respx.mock(base_url=_JQ_BASE):
            respx.get(f"{_JQ_BASE}/equities/master").mock(
                return_value=httpx.Response(200, json={"data": rows_a}),
            )
            store_a = InMemoryEntityStore()
            async with JQuantsClient(api_key=_JQ_KEY) as client_a:
                await reconcile_from_jquants_master(client=client_a, store=store_a)

        with respx.mock(base_url=_JQ_BASE):
            respx.get(f"{_JQ_BASE}/equities/master").mock(
                return_value=httpx.Response(200, json={"data": rows_b}),
            )
            store_b = InMemoryEntityStore()
            async with JQuantsClient(api_key=_JQ_KEY) as client_b:
                await reconcile_from_jquants_master(client=client_b, store=store_b)

        return _snapshot_issuers(store_a), _snapshot_issuers(store_b)

    state_a, state_b = _async_run(_scenario())
    assert _key_set(state_a) == _key_set(state_b)


@pytest.mark.traces("ENT-RECON-P8")
@pytest.mark.xfail(strict=True, reason="impl pending — ADR-0006 step 1 (Task #85)")
@_HYPOTHESIS_SETTINGS
@given(left=_hypothesis_jq_codes(), right=_hypothesis_jq_codes())
def test_p8_jquants_no_silent_merges_for_disjoint_runs(left: list[str], right: list[str]) -> None:
    """P8: For two record lists R1, R2 with disjoint identifier sets,
    running on R1 then R2 yields the same store state (set-equal on
    (display_name, identifier-set) keys) as running on R2 then R1."""
    disjoint_left = [code for code in left if code not in set(right)]
    disjoint_right = [code for code in right if code not in set(left)]

    if not disjoint_left and not disjoint_right:
        return  # vacuously true

    async def _scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        rows_l = [_jq_row(code=c, company_name=f"L {c}") for c in disjoint_left]
        rows_r = [_jq_row(code=c, company_name=f"R {c}") for c in disjoint_right]

        # left then right
        with respx.mock(base_url=_JQ_BASE):
            route = respx.get(f"{_JQ_BASE}/equities/master")
            route.mock(return_value=httpx.Response(200, json={"data": rows_l}))
            store_lr = InMemoryEntityStore()
            async with JQuantsClient(api_key=_JQ_KEY) as client:
                await reconcile_from_jquants_master(client=client, store=store_lr)
            route.mock(return_value=httpx.Response(200, json={"data": rows_r}))
            async with JQuantsClient(api_key=_JQ_KEY) as client:
                await reconcile_from_jquants_master(client=client, store=store_lr)

        # right then left
        with respx.mock(base_url=_JQ_BASE):
            route = respx.get(f"{_JQ_BASE}/equities/master")
            route.mock(return_value=httpx.Response(200, json={"data": rows_r}))
            store_rl = InMemoryEntityStore()
            async with JQuantsClient(api_key=_JQ_KEY) as client:
                await reconcile_from_jquants_master(client=client, store=store_rl)
            route.mock(return_value=httpx.Response(200, json={"data": rows_l}))
            async with JQuantsClient(api_key=_JQ_KEY) as client:
                await reconcile_from_jquants_master(client=client, store=store_rl)

        return _snapshot_issuers(store_lr), _snapshot_issuers(store_rl)

    lr, rl = _async_run(_scenario())
    assert _key_set(lr) == _key_set(rl)


@pytest.mark.traces("ENT-RECON-P9", "ENT-RECON-A4")
@pytest.mark.xfail(strict=True, reason="impl pending — ADR-0006 step 1 (Task #85)")
@_HYPOTHESIS_SETTINGS
@given(code=st.integers(min_value=10000, max_value=99999).map(lambda i: f"{i:05d}"))
def test_p9_jquants_conflict_message_is_deterministic(code: str) -> None:
    """P9: For any record r that conflicts with an existing identifier,
    running the loader on r twice yields the same conflicts tuple
    (deterministic conflict message)."""

    async def _scenario() -> tuple[tuple[str, ...], tuple[str, ...]]:
        existing = Issuer(
            id="I" + "f" * 16,
            lei=None,
            jcn=None,
            display_name="Existing",
            identifiers=(Identifier(kind=IdentifierKind.JQUANTS_CODE, value=code),),
        )

        rows = [_jq_row(code=code, company_name="Conflicting")]

        with respx.mock(base_url=_JQ_BASE):
            respx.get(f"{_JQ_BASE}/equities/master").mock(
                return_value=httpx.Response(200, json={"data": rows}),
            )
            store_a = InMemoryEntityStore()
            store_a.upsert_issuer(issuer=existing)
            store_b = InMemoryEntityStore()
            store_b.upsert_issuer(issuer=existing)

            async with JQuantsClient(api_key=_JQ_KEY) as client:
                result_a = await reconcile_from_jquants_master(client=client, store=store_a)
                result_b = await reconcile_from_jquants_master(client=client, store=store_b)

        return result_a.conflicts, result_b.conflicts

    a, b = _async_run(_scenario())
    assert a == b
    assert len(a) == 1


# === Helpers ===


def _snapshot_issuers(store: InMemoryEntityStore) -> dict[str, Any]:
    """Stable snapshot of the issuer table for equality comparison."""
    return {
        issuer_id: issuer.model_dump(mode="json")
        for issuer_id, issuer in sorted(store._issuers.items())
    }


def _key_set(snapshot: dict[str, Any]) -> set[tuple[str, tuple[tuple[str, str], ...]]]:
    """Order-insensitive equality view: drop issuer_id (which is random)."""
    out: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    for issuer in snapshot.values():
        idents = tuple(sorted((ident["kind"], ident["value"]) for ident in issuer["identifiers"]))
        out.add((issuer["display_name"], idents))
    return out


def _extract_provenance_for_jquants(
    result: ReconcilerResult,
    store: InMemoryEntityStore,
    *,
    code: str,
) -> Any:
    """Locate the per-upsert Provenance for a J-Quants record by its code.

    Per spec section D the loader writes Provenance somewhere observable;
    we accept either:
      - result.provenance (a tuple/list of Provenance, length ==
        upserted, in input order), OR
      - a sentinel attached to the upserted Issuer.

    The implementor in step 2 picks one path; this helper recognises both.
    Returns None if neither is present (which fails the test).
    """
    provenance_seq: Iterable[Any] | None = getattr(result, "provenance", None)
    if provenance_seq is not None:
        for prov in provenance_seq:
            if getattr(prov, "source", None) == Source.JQUANTS:
                return prov
    issuer = store.lookup_issuer(kind=IdentifierKind.JQUANTS_CODE, value=code)
    if issuer is None:
        return None
    return getattr(issuer, "provenance", None)
