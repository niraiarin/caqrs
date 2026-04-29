# Data Integration Layer — TyDD + TDD Design Specification

This is the design specification for `caqrs.entities` — the cross-source canonical-issuer +
relation layer the survey concluded CAQRS needs. The spec follows the TyDD + TDD discipline
from `~/.claude/CLAUDE.md`:

- **Step 0**: Test list (declarative goals)
- **Step 1**: Failing examples first (Given-When-Then per goal)
- **Step 2**: Public API + Pydantic types
- **Step 3**: Type checker exposes missing conditions
- **Step 4**: Minimal implementation flips red → green
- **Step 5**: Property-based tests for round-trip / commutativity / temporal invariants

The spec does **not** ship code; it ships the contract and the test list. Implementation
slices land in subsequent PRs (each one one ↔ a few test-list rows at a time).

---

## §A. Acceptance criteria (the user-visible promise)

The integration layer is "done" when these are all true:

| # | Acceptance criterion | Verifiable by |
|---|---|---|
| A1 | Given any source-specific identifier (J-Quants `Code`, yFinance ticker, EDINET code, JCN, LEI), the layer returns the canonical `Issuer` for that entity, or `None` if unknown. | Test list T1, T2 |
| A2 | The same canonical `Issuer` is returned no matter which source-specific id is used to look it up. | T3 |
| A3 | Assigning the same external id to two different `Issuer` records is rejected at write time. | T4 |
| A4 | A typed `MarketSeries` for an `Issuer` over a date range merges price observations from J-Quants and yFinance with deterministic source-priority and explicit conflict reporting. | T5, T6 |
| A5 | A `Filing` event log can be appended to and queried by `(issuer, date_range, doc_type_codes)`. Storage is append-only — events are never mutated, only superseded by a corrective event. | T7, T8 |
| A6 | Relations between `Issuer`s (subsidiary, large-shareholding, market-subject-of) carry `(valid_from, valid_to)` time bounds. Querying `subsidiaries_of(issuer, at=date)` returns only relations valid at that date. | T9, T10 |
| A7 | Round-trip preservation: persist any `Issuer + Identifier + MarketSeries + Filing + Relation` graph and reload it from a fresh process with identical structure (modulo schema-version migrations). | T11 |
| A8 | All records carry **provenance** — source, fetched_at timestamp, and original-payload hash — so a future audit can reconstruct exactly which upstream response produced the record. | T12 |

---

## §B. Test list (Step 0 of TyDD)

These are the **declarative verifiable goals** derived from the acceptance criteria. Each
row corresponds to one or more failing examples that drive the implementation.

### Identity + lookup

| Test | Given | When | Then |
|---|---|---|---|
| T1 | An empty `EntityStore` | Lookup by `(Source.JQUANTS, "72030")` | Returns `None` |
| T2 | A store with Toyota's `Issuer` (JCN `1180301018771`, LEI `5493006Z4DXP3JNCAY09`) and identifiers `(JQUANTS, "72030")`, `(EDINET, "E02144")`, `(YFINANCE, "7203.T")` | Lookup by any of those source-specific ids | Returns the same `Issuer` |
| T3 | The store from T2 | Lookup the `Issuer` via JCN, then re-query by LEI | The two results are equal (same `Issuer.id`) |
| T4 | The store from T2 | Try to register `(JQUANTS, "72030")` for a different `Issuer` | Raises `IdentifierConflictError` |

### Multi-source temporal query

| Test | Given | When | Then |
|---|---|---|---|
| T5 | An `Issuer` with J-Quants OHLCV for 2025-06-01..2025-06-30 and yFinance OHLCV for the overlapping range, both daily, no missing days | `get_market_series(issuer, kind=DAILY_CLOSE, range=2025-06)` with source-priority `(JQUANTS, YFINANCE)` | Returns a series whose every point has `source=JQUANTS` (since J-Quants covers the entire range) |
| T6 | Same as T5 but yFinance has 5 trading days that J-Quants does **not** cover (e.g. yFinance ADR data for Toyota US) | Same query with source-priority `(JQUANTS, YFINANCE)` | Returns a series that is mostly J-Quants but the 5 ADR-only days carry `source=YFINANCE`. The result also exposes a `conflict_log` field listing any (date, kind) tuples where multiple sources disagreed on value. |

### Filing event log

| Test | Given | When | Then |
|---|---|---|---|
| T7 | An empty store; an Issuer | `append_filing(filing=...)` once, then `filings_for(issuer, range, doc_type_codes=("120",))` | Returns a tuple containing only the appended filing |
| T8 | A store with two filings on the same date for the same issuer (e.g. an original 四半期報告書 and its 訂正報告書) | `filings_for(...)` over the date range | Returns both, in submit-time order, with the corrective filing's `parent_doc_id` referencing the original |

### Relation graph

| Test | Given | When | Then |
|---|---|---|---|
| T9 | A store with a `subsidiary_of` relation: A is subsidiary of B from 2020-04-01 to 2024-03-31; A is subsidiary of C from 2024-04-01 onwards | `subsidiaries_of(B, at=date(2024, 3, 31))` | Returns `(A,)` |
| T10 | Same store | `subsidiaries_of(B, at=date(2024, 4, 1))` | Returns `()` (A is no longer subsidiary of B at that date) |

### Round-trip

| Test | Given | When | Then |
|---|---|---|---|
| T11 | A populated store written to a DuckDB file, then a fresh `EntityStore.from_path(...)` from a new process | `list_all_issuers()`, `list_all_relations()`, `list_all_filings()` from the reloaded store | Match the original byte-for-byte (after sorting on stable keys) |

### Provenance

| Test | Given | When | Then |
|---|---|---|---|
| T12 | A `MarketSeries` populated from a J-Quants response | Inspect any point | `point.provenance.source == Source.JQUANTS`, `point.provenance.fetched_at` is the original fetch time, `point.provenance.payload_hash` matches `hashlib.sha256(canonical_json(original_response)).hexdigest()` |

### Property-based tests (Step 5)

These supplement the example-based tests for invariants that can be checked by Hypothesis:

| Property | Statement |
|---|---|
| P1 | For any non-empty `MarketSeries` `s`, sorting `s.points` by `timestamp` is idempotent (i.e. the layer always returns time-sorted points) |
| P2 | For any `Issuer` `i` and any subset `S` of its identifiers, `lookup(any element of S) == i` (lookup is deterministic under permutation) |
| P3 | For any `Filing` `f` written then read back via `append + filings_for`, the loaded `Filing` equals `f` (round-trip preservation) |
| P4 | For any `Relation` `r` with `valid_from < valid_to`, `r` is returned by `relations_at(t)` if and only if `valid_from <= t < valid_to` (half-open interval) |
| P5 | For two valid `Issuer` records `a, b` with disjoint identifier sets, merging them via `EntityStore.merge_issuers(a, b)` yields one canonical `Issuer` whose identifier set is `a.identifiers ∪ b.identifiers` (idempotent on existing merges) |

---

## §C. Public API (Step 2 of TyDD)

### C.1 Source enum

```python
class Source(StrEnum):
    """Origin of a piece of CAQRS-ingested data.

    Adding a new source = adding a new member; downstream code that
    pattern-matches on Source must add a branch (mypy + ruff
    enforce exhaustiveness).
    """

    JQUANTS = "jquants"
    YFINANCE = "yfinance"
    POLYMARKET_CLOB = "polymarket_clob"
    POLYMARKET_GAMMA = "polymarket_gamma"
    POLYMARKET_ARCHIVE = "polymarket_archive"
    EDINET = "edinet"
    EDINETDB = "edinetdb"

    # Reserved for future slices — not a member yet, but listed in the
    # design spec so the type system surfaces the gap when one of these
    # adapters lands:
    #   NEWS_*  (news adapter; deferred)
    #   GLEIF   (LEI master; deferred)
```

### C.2 Identifier kinds

```python
class IdentifierKind(StrEnum):
    """Distinct identifier *namespaces*. A single Source can emit
    several IdentifierKinds (e.g. EDINET emits both edinet_code and
    JCN per filing); reconciliation joins on the kind, not the source.
    """

    JQUANTS_CODE = "jquants_code"        # 5-digit "72030"
    SEC_CODE = "sec_code"                # 4-digit "7203" (J-Quants minus check)
    YFINANCE_TICKER = "yfinance_ticker"  # "7203.T"
    EDINET_CODE = "edinet_code"          # "E02144"
    JCN = "jcn"                          # "1180301018771" (13-digit)
    LEI = "lei"                          # "5493006Z4DXP3JNCAY09"
    POLYMARKET_TOKEN = "polymarket_token"
```

### C.3 Issuer + Identifier types

```python
from typing import Annotated
from pydantic import Field
from caqrs.schemas.common import StrictBaseModel

IssuerId = Annotated[str, Field(pattern=r"^I[0-9a-f]{16}$")]  # canonical 16-hex


class Identifier(StrictBaseModel):
    """One source-specific id pointing at one canonical Issuer.

    The combination (kind, value) is unique across the store: assigning
    the same identifier to two Issuers is the IdentifierConflictError
    case in T4.
    """

    kind: IdentifierKind
    value: str = Field(min_length=1, max_length=64)


class Issuer(StrictBaseModel):
    """Canonical legal-entity record.

    `id` is CAQRS-internal (16-hex), assigned at first encounter.
    `lei` and `jcn` are world-wide canonical identifiers (when known)
    and double as preferred reconciliation anchors. `identifiers` is
    the full source-side cross-walk; downstream queries always start
    from this tuple.

    `display_name` is the user-facing label; CAQRS does **not** treat
    it as canonical (names change, are translated, are reorganised),
    so it never participates in lookups.
    """

    id: IssuerId
    lei: str | None = Field(default=None, pattern=r"^[A-Z0-9]{20}$")
    jcn: str | None = Field(default=None, pattern=r"^\d{13}$")
    display_name: str = Field(min_length=1, max_length=256)
    identifiers: tuple[Identifier, ...]
```

### C.4 Provenance

```python
class Provenance(StrictBaseModel):
    """Why the agent should believe this row.

    Required on every persisted record so an audit can reconstruct
    which upstream response produced it.
    """

    source: Source
    fetched_at: datetime  # tz-aware, UTC
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")  # sha256 hex

    # Optional cross-link to the cache row that holds the raw payload
    # (yfinance / edinetdb caches keyed by the same hash). Absence is
    # not an error — old data may pre-date the cache.
    cache_key: str | None = None
```

### C.5 Time-series + filing types

```python
class MarketSeriesKind(StrEnum):
    DAILY_OPEN = "daily_open"
    DAILY_HIGH = "daily_high"
    DAILY_LOW = "daily_low"
    DAILY_CLOSE = "daily_close"
    DAILY_ADJ_CLOSE = "daily_adj_close"
    DAILY_VOLUME = "daily_volume"
    POLYMARKET_MIDPOINT = "polymarket_midpoint"
    # Reserved: NEWS_SENTIMENT (deferred)


class MarketPoint(StrictBaseModel):
    timestamp: datetime  # tz-aware UTC
    value: Decimal
    provenance: Provenance


class MarketSeries(StrictBaseModel):
    issuer_id: IssuerId
    kind: MarketSeriesKind
    points: tuple[MarketPoint, ...]
    # Conflict log surfaces (timestamp, kind) tuples where ≥2 sources
    # disagreed on the value beyond a tolerance. The query layer
    # picks one per priority; the conflict log preserves what was
    # discarded so an auditor can see the choice.
    conflict_log: tuple[ConflictRecord, ...] = ()


class ConflictRecord(StrictBaseModel):
    timestamp: datetime
    chosen: MarketPoint
    discarded: tuple[MarketPoint, ...]


class Filing(StrictBaseModel):
    """One disclosure event keyed by its EDINET docID (the only
    cross-source-stable identifier we have for filings)."""

    issuer_id: IssuerId
    doc_id: str = Field(min_length=1, max_length=20)
    doc_type_code: str
    submitted_at: datetime  # tz-aware UTC; converts JST naive at parse time
    parent_doc_id: str | None = None  # 訂正報告書 chains
    provenance: Provenance
```

### C.6 Relation graph types

```python
class RelationKind(StrEnum):
    """Closed enum — adding a kind requires a code change, on
    purpose so consumers stay aware of what edges exist."""

    SUBSIDIARY_OF = "subsidiary_of"           # from EDINET subsidiaryEdinetCode
    LARGE_SHAREHOLDER_OF = "large_shareholder_of"  # from 大量保有報告書 (doc_type 080)
    PUBLIC_TENDER_TARGET = "public_tender_target"  # from 公開買付 family
    POLYMARKET_SUBJECT = "polymarket_subject"  # market is about this Issuer


class Relation(StrictBaseModel):
    """A time-bounded edge between two Issuers.

    Half-open interval semantics: the relation is valid for
    ``[valid_from, valid_to)``. ``valid_to=None`` means "currently
    valid, end unknown".
    """

    from_id: IssuerId
    to_id: IssuerId
    kind: RelationKind
    valid_from: datetime
    valid_to: datetime | None = None
    provenance: Provenance
```

### C.7 EntityStore protocol

```python
from typing import Protocol


class EntityStore(Protocol):
    """The integration layer's public surface.

    Two implementations are expected at first ship:
      - InMemoryEntityStore (for tests, ad-hoc REPL)
      - DuckDbEntityStore (the production backend)

    Pluralised methods return tuples (immutable). No methods raise
    on cache miss — they return None / () instead, mirroring the
    convention in caqrs.data.*.
    """

    # Identity
    def get_issuer(self, *, issuer_id: IssuerId) -> Issuer | None: ...
    def lookup_issuer(self, *, kind: IdentifierKind, value: str) -> Issuer | None: ...
    def upsert_issuer(self, *, issuer: Issuer) -> Issuer: ...
    def merge_issuers(self, *, keep: IssuerId, drop: IssuerId) -> Issuer: ...

    # Series
    def get_market_series(
        self,
        *,
        issuer_id: IssuerId,
        kind: MarketSeriesKind,
        range_: tuple[datetime, datetime],
        source_priority: tuple[Source, ...],
    ) -> MarketSeries: ...
    def append_market_points(self, *, points: Sequence[MarketPoint]) -> None: ...

    # Filings
    def append_filing(self, *, filing: Filing) -> None: ...
    def filings_for(
        self,
        *,
        issuer_id: IssuerId,
        range_: tuple[datetime, datetime],
        doc_type_codes: Sequence[str] | None = None,
    ) -> tuple[Filing, ...]: ...

    # Relations
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
```

### C.8 Errors

```python
class EntityStoreError(Exception):
    """Base for typed integration-layer failures."""


class IdentifierConflictError(EntityStoreError):
    """Raised by upsert_issuer when an identifier is already
    associated with a different Issuer (T4)."""

    def __init__(
        self,
        *,
        kind: IdentifierKind,
        value: str,
        existing_issuer_id: IssuerId,
        proposed_issuer_id: IssuerId,
    ) -> None: ...


class UnknownIssuerError(EntityStoreError):
    """Raised by append_market_points / append_filing /
    append_relation when the referenced issuer_id does not exist
    in the store. Forces the caller to upsert the Issuer first
    (no auto-creation — the integrity boundary is explicit)."""
```

---

## §D. Holes the type checker enforces (Step 3 of TyDD)

These are deliberate type-level gaps that make "missing implementation" surface as a mypy
error rather than a silent fallthrough:

1. **`Source` exhaustiveness**: every `match` over `Source` ends with
   `case _: assert_never(s)`. Adding a new Source member forces every site to update.
2. **`MarketSeriesKind` exhaustiveness**: same pattern for the union of `kind`s in series
   queries. Future `NEWS_SENTIMENT` will surface as an unreachable branch the moment it's
   added to the enum.
3. **`Vector layer` reservation**: `caqrs.entities.vector` is a planned subpackage; it is
   referenced from a `from caqrs.entities.vector import EmbeddingProvider` in the design
   doc but **not** imported by any production code. When a slice adds news embeddings, the
   import lights up and mypy guides the implementor.
4. **`Provenance` is required**: `MarketPoint`, `Filing`, `Relation` all have `provenance:
   Provenance` (not `Provenance | None`). A test fixture that forgets to set it fails at
   construction; an upstream-adapter migration that drops the field fails to type-check.
5. **`Identifier.value` constraints**: `min_length=1, max_length=64` rejects empty or
   absurdly long values at construction. Reconciliation bugs that string-concatenate
   identifiers surface as `ValidationError` in the test that triggered them.

---

## §E. Implementation phasing (the roadmap inside the design)

The spec deliberately covers more surface than one PR can land. Slices, in order:

### Phase E1 — Core types + InMemoryEntityStore (test-only)

- Ship `caqrs.entities.types` (Source / IdentifierKind / Issuer / Identifier / Provenance /
  MarketSeries / Filing / Relation / errors).
- Ship `caqrs.entities.in_memory.InMemoryEntityStore` — pure Python, no persistence.
- Failing tests T1–T12 (in-memory only) drive the implementation.
- Property-based tests P1–P5 land alongside.

This is the smallest end-to-end slice that can be reviewed independently. ~2 PRs.

### Phase E2 — DuckDB-backed implementation

- `caqrs.entities.duckdb.DuckDbEntityStore` implementing the same `EntityStore` protocol.
- DuckDB schema: `issuers`, `identifiers`, `market_points`, `filings`, `relations`,
  `provenance` tables.
- Round-trip test T11 + cross-instance read.
- Migration story: `entities_schema_version` table, idempotent `init()`.

This is the production backend. Adds `duckdb` as `caqrs[entities]` optional extra. ~1 PR.

### Phase E3 — Reconciliation seed loader

- `caqrs.entities.reconciliation` module with one helper per source: e.g.
  `reconcile_from_jquants_master(client, store)` walks `JQuantsClient.list_master()` and
  upserts `Issuer` + `Identifier(JQUANTS_CODE, ...)` rows.
- Same for EDINET DB (`/companies` walk → upsert `Issuer` + `EDINET_CODE` + `JCN` + maybe
  `SEC_CODE`).
- Optional GLEIF LEI loader (downloads the public bulk file). Deferred to Phase E5 if not
  needed.

This bootstraps a populated store. ~1 PR.

### Phase E4 — CycleRunner integration

- New `EntityResolver` agent input on `Observer`: receives an `EntityStore`, looks up
  `Issuer` for each ticker / EDINET code in the universe, returns a unified
  `(issuer, current_market_series, latest_fundamentals, recent_filings)` bundle.
- Cycle runner gains an `entity_store: EntityStore | None` ctor arg (optional, like
  `policy_gateway_config`). When provided, all data fetches resolve through Issuer first.

Replaces the per-source ad-hoc helpers in the Observer. ~1 PR.

### Phase E5 — Vector layer (deferred)

Reserved hole. Lands when news / text embedding becomes a research axis.

---

## §F. Anti-goals (what this layer is **not**)

To prevent scope creep:

1. **Not a query-language frontend**. There is no SQL, Cypher, or text-to-query interface.
   Agents call typed Python helpers. (See survey §4.1: anti-pattern.)
2. **Not a streaming engine**. Append-only batch ingestion. Real-time / push subscriptions
   are out of scope.
3. **Not a feature store**. We persist source-faithful raw observations + provenance, not
   per-experiment ML features. Feature engineering belongs in caller code or a separate
   layer.
4. **Not a portfolio store**. `caqrs.execution.PaperBroker` already owns position state;
   `caqrs.entities` describes the world, not our position in it.
5. **Not a fact-checking layer**. We record what each source said with provenance. We do
   not adjudicate truth across sources beyond the deterministic source-priority tiebreak
   in `get_market_series`.

---

## §G. Risks + mitigations

| Risk | Mitigation |
|---|---|
| **Identifier drift**: EDINET codes occasionally get reassigned (rare but documented in `caqrs.data.edinet` survey notes). | `Identifier` carries no `valid_from / valid_to` in v1, but a future `IdentifierVersion` extension can layer it without breaking the protocol. |
| **JCN ↔ EDINET-code conflicts**: a single legal entity can have separate EDINET codes per fund. | The `Issuer` model treats funds as their own `Issuer` (matching how EDINET DB does it); the seed loader does not auto-merge funds with their parent corp. |
| **DuckDB version pinning**: DuckDB minor releases occasionally change query-plan output. | Pin to a tested minor version in `pyproject.toml`; integration tests verify the schema matches. |
| **Observer over-fetching**: a naive Observer might walk `list_all_issuers()` per cycle. | The `Observer` interface in Phase E4 takes an explicit universe argument; bulk walks are a separate operator (CLI / cron) responsibility. |

---

## §H. Traceability

| Acceptance criterion | Test list | Public API surface |
|---|---|---|
| A1 | T1, T2 | `EntityStore.lookup_issuer` |
| A2 | T3 | `EntityStore.lookup_issuer` (deterministic) |
| A3 | T4 | `EntityStore.upsert_issuer` + `IdentifierConflictError` |
| A4 | T5, T6 | `EntityStore.get_market_series` + `MarketSeries.conflict_log` |
| A5 | T7, T8 | `EntityStore.append_filing` + `EntityStore.filings_for` |
| A6 | T9, T10 | `EntityStore.subsidiaries_of` + `Relation.valid_from/valid_to` |
| A7 | T11 | `DuckDbEntityStore` round-trip |
| A8 | T12 | `Provenance` field on every persisted type |

---

## §I. References

- Survey file: [`01-survey.md`](01-survey.md) — extends the user's prior-art seed.
- Existing CAQRS docs:
  - `docs/ARCHITECTURE.md` (cycle architecture; the layer plugs in at the Observer step)
  - `docs/decisions/0005-policy-gateway-as-pure-function.md` (the typed-protocol pattern
    we replicate here)
  - `docs/research/mercury-survey/00-index.md` (lineage of design choices CAQRS already
    inherits)
- TyDD + TDD discipline: `~/.claude/CLAUDE.md` § "TyDD + TDD 運用".
- LEI standard: ISO 17442; bulk data at `https://www.gleif.org/`
- JCN bulk data: `https://www.houjin-bangou.nta.go.jp/`
