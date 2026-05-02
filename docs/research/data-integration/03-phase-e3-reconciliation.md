# Phase E3 — Reconciliation seed loaders

This is the Phase E3 spec, building on the data-integration roadmap in
[`02-design-spec-tydd.md`](02-design-spec-tydd.md) §E. Phase E1 (in-memory) and Phase E2
(DuckDB-backed) `EntityStore`s are merged. Phase E3 adds the **seed loaders** that bootstrap
a populated `EntityStore` from upstream sources by walking each source's master / list
endpoint and upserting `Issuer` + `Identifier` rows with full provenance.

The spec follows the same TyDD + TDD discipline as file 02:

- **Step 0**: declarative test list (T20–T30) + property tests (P6–P9)
- **Step 1**: failing examples (Given-When-Then per row)
- **Step 2**: public API + Pydantic types
- **Step 3**: type checker exposes missing conditions
- **Step 4**: minimal implementation flips red → green (lands in #85)
- **Step 5**: property-based tests for idempotency + source-order independence

The spec does **not** ship code; it ships the contract and the test list. Implementation
lands in PR #85 (one slice per source).

---

## §A. Acceptance criteria

The reconciliation layer is "done" when these are all true:

| # | Criterion | Verifiable by |
|---|---|---|
| ENT-RECON-A1 | A reconciler accepts an `EntityStore` + source client and emits `N` `Issuer` + `Identifier` upserts, returning a `ReconcilerResult(upserted, skipped, conflicts, dry_run)` summary. | T20, T21, T22 |
| ENT-RECON-A2 | Re-running the same reconciler on identical input data is a **no-op**: existing `Issuer` rows are updated only when `display_name` differs, no duplicate `Identifier` rows are written, and `result.upserted` reflects only the records whose canonical state actually changed. | T23, P6 |
| ENT-RECON-A3 | Each upserted record carries `Provenance(source=<correct enum>, fetched_at=<call time, UTC tz-aware>, payload_hash=sha256(canonical_json(raw_record)))`. | T24 |
| ENT-RECON-A4 | When a source-specific id collides with a **different** `Issuer`'s existing identifier, the loader catches `IdentifierConflictError`, appends a structured message to `result.conflicts`, and continues with the next record (the failing batch row is **not** persisted; later rows still run). | T25, P9 |
| ENT-RECON-A5 | Loaders accept `dry_run: bool = False`. When `True`, the loader walks the source as usual but never calls `store.upsert_issuer`; the returned `ReconcilerResult.upserted` reflects how many records *would have been* upserted, and `result.dry_run is True`. | T26 |
| ENT-RECON-A6 | The id-taxonomy emitted per loader matches the source's contract: `reconcile_from_jquants_master` populates `JQUANTS_CODE` (5-digit) + `SEC_CODE` (4-digit derived) per row; `reconcile_from_edinetdb_companies` populates `EDINET_CODE` + `JCN` (when present) + optionally `SEC_CODE`. The GLEIF LEI loader (deferred to E5) would populate `LEI`. | T27, T28, T29 |

---

## §B. Test list (Step 0 of TyDD)

These are the declarative verifiable goals. Each row corresponds to one or more failing
examples driving #85.

### Per-source happy path (T20–T22)

| Test | Given | When | Then |
|---|---|---|---|
| ENT-RECON-T20 | A fresh `InMemoryEntityStore` and a `JQuantsClient` stub whose `list_master()` yields three rows (Toyota / Sony / NTT) | `await reconcile_from_jquants_master(client=stub, store=store)` | Returns `ReconcilerResult(upserted=3, skipped=0, conflicts=(), dry_run=False)`. The store contains 3 `Issuer` rows; each has both `(JQUANTS_CODE, "<5-digit>")` and `(SEC_CODE, "<4-digit>")` identifiers. |
| ENT-RECON-T21 | A fresh store and an `EdinetDbClient` stub whose `list_companies(per_page=500)` paginates through 1200 rows (3 pages) | `await reconcile_from_edinetdb_companies(client=stub, store=store)` | Returns `ReconcilerResult(upserted=1200, skipped=0, conflicts=(), dry_run=False)`. Every row's `Issuer` carries an `(EDINET_CODE, ...)` identifier; rows whose source record had a JCN also carry `(JCN, ...)`. |
| ENT-RECON-T22 | A fresh store and a (deferred) GLEIF bulk-file fixture | (deferred) `await reconcile_from_gleif_lei(...)` | (deferred to Phase E5) Documents the contract; the test is a `pytest.skip("Phase E5")` placeholder so the spec stays traceable. |

### Idempotency (T23)

| Test | Given | When | Then |
|---|---|---|---|
| ENT-RECON-T23 | A store already populated by one J-Quants run | `await reconcile_from_jquants_master(...)` again on the same `list_master()` payload | Returns `ReconcilerResult(upserted=0, skipped=N, conflicts=(), dry_run=False)`. No duplicate identifier rows are written; existing `Issuer` rows are not touched (verified by snapshotting the store before and comparing equality after). |

### Provenance population (T24)

| Test | Given | When | Then |
|---|---|---|---|
| ENT-RECON-T24 | A J-Quants stub with a single fixed row and `freezegun` pinning `datetime.now(UTC)` to `2026-05-02T00:00:00Z` | `await reconcile_from_jquants_master(...)` | The persisted `Issuer`'s associated `Provenance` (e.g. on a sentinel `MarketPoint` if the loader writes one, otherwise on the `Issuer`-attached provenance per #85's design) has `source == Source.JQUANTS`, `fetched_at == 2026-05-02T00:00:00+00:00`, `payload_hash == sha256(canonical_json(raw_row)).hexdigest()`. |

### Conflict handling (T25)

| Test | Given | When | Then |
|---|---|---|---|
| ENT-RECON-T25 | A store pre-populated with `Issuer` X carrying `(JQUANTS_CODE, "72030")`, then a J-Quants stub yielding `(JQUANTS_CODE, "72030")` for a **different** legal entity Y (synthetic, not Toyota) | `await reconcile_from_jquants_master(...)` | Returns `ReconcilerResult(upserted=0, skipped=1, conflicts=(<one structured message naming kind=JQUANTS_CODE, value='72030', existing_issuer_id, proposed_issuer_id>,), dry_run=False)`. Issuer X is unchanged; Issuer Y is **not** persisted. |

### Dry-run mode (T26)

| Test | Given | When | Then |
|---|---|---|---|
| ENT-RECON-T26 | A fresh store and the J-Quants three-row stub from T20 | `await reconcile_from_jquants_master(client=stub, store=store, dry_run=True)` | Returns `ReconcilerResult(upserted=3, skipped=0, conflicts=(), dry_run=True)`. The store is **byte-for-byte unchanged** (`list_all_issuers() == ()`). |

### Source-specific id taxonomy (T27, T28, T29)

| Test | Given | When | Then |
|---|---|---|---|
| ENT-RECON-T27 | A J-Quants stub with one row (`Code="72030"`) | `await reconcile_from_jquants_master(...)` | The persisted `Issuer.identifiers` contains exactly two entries: `Identifier(kind=JQUANTS_CODE, value="72030")` and `Identifier(kind=SEC_CODE, value="7203")`. No `EDINET_CODE` is invented. |
| ENT-RECON-T28 | An EDINET DB stub with one row (`edinet_code="E02144"`, `jcn="1180301018771"`, `sec_code="7203"`) | `await reconcile_from_edinetdb_companies(...)` | The persisted `Issuer.identifiers` contains `(EDINET_CODE, "E02144")`, `(JCN, "1180301018771")`, and `(SEC_CODE, "7203")`. |
| ENT-RECON-T29 | An EDINET DB stub with one row that has `edinet_code="E12345"` but `jcn=None` and `sec_code=None` (legitimate: trust funds and SPCs sometimes lack one or both) | `await reconcile_from_edinetdb_companies(...)` | The persisted `Issuer.identifiers` contains exactly `(EDINET_CODE, "E12345")` — no `(JCN, "")`, no `(SEC_CODE, "")`. `result.upserted == 1`, `result.skipped == 0`. |

### Batch boundaries + transient errors (T30)

| Test | Given | When | Then |
|---|---|---|---|
| ENT-RECON-T30 | An EDINET DB stub configured to paginate at exactly 500 rows per page; the seed yields 1500 rows total. Page 2 raises `httpx.ReadTimeout` once and succeeds on the underlying client's retry. | `await reconcile_from_edinetdb_companies(...)` | The reconciler walks all 3 pages with no internal retry of its own (it leans on the client's `httpx.Transport` retry); final `ReconcilerResult.upserted == 1500`. If the client surfaces the timeout (no retry), the reconciler propagates the exception — it is **one-shot**, callers are expected to re-run. |

### Property-based tests (Step 5)

These supplement the example-based tests for invariants Hypothesis can check.

| Property | Statement |
|---|---|
| ENT-RECON-P6 | For any list of source records `R` such that `len(R) ≤ 50`, running the loader twice in sequence on a single store yields the same store state as running it once (idempotency under repeated runs). |
| ENT-RECON-P7 | For any list `R` and any permutation `π(R)`, the resulting store state (sorted on stable keys) is equal (source-order independence within a single run). |
| ENT-RECON-P8 | For two lists `R₁`, `R₂` of records covering **disjoint** identifier sets, running the loader on `R₁` then `R₂` yields the same store state as running it on `R₂` then `R₁` (no silent merges between runs of disjoint records). |
| ENT-RECON-P9 | For any record `r` that conflicts with an existing identifier, the loader's behaviour is deterministic: the same input always produces the same `ReconcilerResult.conflicts` message and never persists `r`. |

---

## §C. Public API (Step 2 of TyDD)

```python
from typing import Protocol

from caqrs.data.edinetdb import EdinetDbClient
from caqrs.data.jquants import JQuantsClient
from caqrs.entities.protocol import EntityStore
from caqrs.schemas.common import StrictBaseModel


class ReconcilerResult(StrictBaseModel):
    """Outcome of one reconciler run.

    The result is intentionally narrow: counts + conflict log, plus a flag
    that tells the caller whether the store was actually mutated. The
    detailed audit trail lives on the `Provenance` rows attached to each
    upsert (see §D).
    """

    upserted: int
    skipped: int
    conflicts: tuple[str, ...]  # one structured message per IdentifierConflictError
    dry_run: bool


async def reconcile_from_jquants_master(
    *,
    client: JQuantsClient,
    store: EntityStore,
    dry_run: bool = False,
) -> ReconcilerResult:
    """Walk ``client.list_master()`` and upsert one Issuer per row.

    Per-row identifier set: ``(JQUANTS_CODE, <5-digit code>)`` plus
    ``(SEC_CODE, <4-digit code>)`` derived by stripping the trailing
    check digit. ``display_name`` is taken from the source's
    ``CompanyName`` field.
    """
    ...


async def reconcile_from_edinetdb_companies(
    *,
    client: EdinetDbClient,
    store: EntityStore,
    dry_run: bool = False,
) -> ReconcilerResult:
    """Paginate ``client.list_companies(per_page=500)`` to exhaustion and
    upsert one Issuer per row.

    Per-row identifier set: ``(EDINET_CODE, <code>)`` always; plus
    ``(JCN, <13-digit>)`` when the source row carries a non-empty JCN;
    plus ``(SEC_CODE, <4-digit>)`` when the source row carries a
    sec_code (typically only listed companies).
    """
    ...


# GLEIF LEI loader: deferred to Phase E5. The spec preserves the
# placeholder so the type system surfaces the gap when GLEIF lands.
#
# async def reconcile_from_gleif_lei(
#     *,
#     bulk_file_path: Path,
#     store: EntityStore,
#     dry_run: bool = False,
# ) -> ReconcilerResult: ...
```

### Batch / pagination contract

| Source | Batch size | Notes |
|---|---|---|
| J-Quants `list_master()` | client-driven (~4000 codes in a single page; client already streams them) | The reconciler iterates the async generator; no second-level batching. |
| EDINET DB `list_companies()` | `per_page=500` (max permitted by upstream) | Reconciler advances pages until an empty page or a `next_page=None` token. |
| GLEIF bulk file | streaming SAX parse, ~2.5M LEIs | Deferred (Phase E5). |

---

## §D. Behaviour contract

For each loader, the per-record loop is:

1. Read the next source record.
2. Compute `provenance = Provenance(source=<loader's source>, fetched_at=datetime.now(UTC), payload_hash=sha256(canonical_json(record)).hexdigest())`. The `fetched_at` is captured **once per call** at the top of the loader and reused for all records in that run, so re-running the same source data on a different day gives a fresh provenance row.
3. Build the `Identifier` tuple from the source-specific fields (see §C per-row identifier sets).
4. Build the `Issuer` in-memory: assign a fresh `IssuerId` (16-hex) only if no existing `Issuer` already binds any of the new identifiers; otherwise reuse the existing `Issuer.id`.
5. If `dry_run`:
   - Increment `upserted` and continue. Do **not** call `store.upsert_issuer`.
6. Else:
   - Call `store.upsert_issuer(issuer=...)`.
   - On `IdentifierConflictError`: format a one-line structured message (`f"{kind.value} '{value}' already bound to {existing_issuer_id}, refused {proposed_issuer_id}"`), append to `conflicts`, increment `skipped`, continue.
   - On success where the existing `Issuer` was unchanged (idempotent re-run): increment `skipped`, continue.
   - On success with a real change: increment `upserted`, continue.
7. Records that lack any usable canonical id (e.g. EDINET row with no `edinet_code` — currently rare to nonexistent; defensive only) are logged and counted in `skipped`.

After the loop, return `ReconcilerResult(upserted=u, skipped=s, conflicts=tuple(conflicts), dry_run=dry_run)`.

The reconciler is **one-shot**: it does not retry network errors itself. The injected
`JQuantsClient` / `EdinetDbClient` already carries an `httpx.AsyncHTTPTransport(retries=3)`
in production wiring; a transient-error escape from the client is treated as fatal and
propagates. Callers re-run the loader; idempotency (ENT-RECON-A2) makes that safe.

---

## §E. Holes the type checker enforces (Step 3 of TyDD)

These are deliberate type-level gaps that surface "missing implementation" as a mypy error
rather than a silent fallthrough:

1. **`IdentifierKind` exhaustiveness**: each loader builds its identifier tuple via a closed
   `match` over an inline literal kind set; adding a new kind to the enum (e.g. `LEI` when
   GLEIF lands) lights up `assert_never` in any loader that does not yet emit it.
2. **`Source` enum membership**: each loader's emitted `Provenance.source` is a literal
   (`Source.JQUANTS` / `Source.EDINETDB`); a typo (`Source.JQUANT`) fails to type-check.
3. **`ReconcilerResult` is `frozen + extra=forbid`** (inherits from `StrictBaseModel`): a
   future field cannot sneak in via attribute assignment, and a caller mistyping a field
   name in test fixtures fails at construction.
4. **`dry_run: bool = False` is a keyword-only argument** (`*` before it in the signature):
   positional `True` cannot be passed by accident.
5. **Async-only contract**: both loaders are `async def`. A synchronous call site fails
   `await`-checks, so the loader cannot be misused from a non-async caller.

---

## §F. Out of scope (deferred to Phase E4 / E5)

To keep #85 scoped to one PR:

1. **CycleRunner integration** — `EntityResolver` lookup wiring lives in Phase E4 (per file
   02 §E4). #85 ships only the standalone CLI / scripted entrypoints.
2. **Real GLEIF bulk-file download + parse** — Phase E5. The placeholder in §C documents the
   intended signature so the gap is visible.
3. **yFinance ticker → Issuer reconciliation** — yFinance has no master endpoint; the only
   way to learn its tickers is to ask for one at a time. Reconciliation happens at observer
   time (Phase E4), not at seed time.
4. **Identifier valid-time tracking** — `Identifier` carries no `(valid_from, valid_to)` in
   v1 (see file 02 §G "Identifier drift"); a future `IdentifierVersion` extension can layer
   it without breaking the protocol.
5. **Auto-merge of EDINET fund codes with their parent corporation** — file 02 §G "JCN ↔
   EDINET-code conflicts" defers this to a separate, manually-triaged operator step.

---

## §G. Risks + mitigations

| Risk | Mitigation |
|---|---|
| Network errors during a multi-page walk | Caller-supplied client carries `httpx` retry; reconciler is one-shot. Idempotency (ENT-RECON-A2) makes re-runs safe. |
| EDINET DB quota burn (100 req/day on the free tier) | `dry_run=True` for development; live tests gated behind `CAQRS_RUN_LIVE_EDINETDB=1`. |
| Identifier reassignment (EDINET codes change rarely but do) | Conflicts are collected, **not raised**, and surfaced via `ReconcilerResult.conflicts` for human review. Re-running after a conflict is resolved (e.g. by `merge_issuers`) is safe. |
| Long J-Quants `list_master` payload (~4000 rows ≈ 1.5 MB JSON) | Client already streams; reconciler iterates the async generator in O(1) memory. |
| Provenance hash explosion (every row hashes its raw payload) | sha256 is fast; canonical-json normalisation is the same path the rest of CAQRS uses (see file 02 §C.4). No measurable hot path. |

---

## §H. Traceability

| Acceptance criterion | Tests | Property tests |
|---|---|---|
| ENT-RECON-A1 | T20, T21, T22 | — |
| ENT-RECON-A2 | T23 | P6 |
| ENT-RECON-A3 | T24 | — |
| ENT-RECON-A4 | T25 | P9 |
| ENT-RECON-A5 | T26 | — |
| ENT-RECON-A6 | T27, T28, T29 | — |
| (cross-cutting: ordering, batches) | T30 | P7, P8 |

REQ-IDs `ENT-RECON-A1..A6`, `ENT-RECON-T20..T30`, `ENT-RECON-P6..P9` are registered in
`docs/requirements/registry.yaml` with `status: deferred` and the note "Phase E3
implementation pending #85". #85 fills `types: []` and `tests: []` and flips status to
`formalized` slice by slice.

---

## §I. References

- File 02: [`02-design-spec-tydd.md`](02-design-spec-tydd.md) §E (phasing), §G (risks), §C
  (the types this loader composes).
- ADR-0006 (`docs/decisions/0006-two-step-tdd-dispatch.md`): Phase E3 implementation #85
  follows the two-step dispatch.
- ADR-0007 (`docs/decisions/0007-verifier-report-artifact.md`): #85's PR will attach a
  verifier artifact under `docs/reviews/`.
- `caqrs.entities.protocol.EntityStore`: the `Protocol` the loaders consume (file 02 §C.7).
- `caqrs.data.jquants.JQuantsClient.list_master`: the J-Quants endpoint the seed loader
  walks (`src/caqrs/data/jquants/client.py:115`).
- `caqrs.data.edinetdb.EdinetDbClient.list_companies`: the EDINET DB endpoint family
  (`src/caqrs/data/edinetdb/client.py:137`).
- TyDD + TDD discipline: `~/.claude/CLAUDE.md` § "TyDD + TDD 運用".
