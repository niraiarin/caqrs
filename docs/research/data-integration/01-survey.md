# Data Integration Survey — Multi-Modal Time Series + Financial KGs + Identity + Tech

This file consolidates the user-supplied prior-art seed (covering FNSPID / FinMultiTime /
FinDKG / MAGNN family) with four extensions that the seed left underdeveloped: identity
reconciliation, implementation tech comparison, LLM agent patterns, and the CAQRS-specific
position.

---

## §1. User-supplied prior art (verbatim summary, with citations)

### 1.1 The shape of the integration problem

Combining price, news, corporate filings, and earnings into a **single unified time series**
is widely studied but **provably lossy**:

- **Non-commutativity of news**: `N_t1 + N_t2 ≠ N_t2 + N_t1` — order matters for downstream
  inference. Flat 1-D series destroys the order semantics.
- **Many-to-many cardinality**: one news item touches many issuers; one issuer generates
  many filings. Reducing to a single per-issuer series collapses these.
- **Lead-lag effects**: information's price impact is delayed and varies by relation type
  (supplier → customer; parent → subsidiary). Captured naturally in a graph, not in
  parallel scalar series.

Mathematical re-formulation the literature settles on:

```
Sequence over time:    {X_t, G_t}_{t=1..T}
  X_t ∈ R^d  : numeric feature vector
  G_t        : graph over entities at time t (nodes ∪ edges with attributes)
```

### 1.2 Multi-modal time-series datasets

| Project | Scope | Citation |
|---|---|---|
| **FNSPID** | 株価 ~30M rows + ニュース ~16M items, time-aligned | https://arxiv.org/abs/2402.06698 |
| **FinMultiTime** | 4-modal (price + news + financials + charts), long-horizon | https://arxiv.org/abs/2506.05019 |
| **FinTextTS** | Text + price aligned for sentiment & forecast | (related family) |

Generic shape: `X_t = (P_t, N_t, F_t, M_t)` where `P_t` is price, `N_t` is news embedding,
`F_t` is fundamentals, `M_t` is market structure.

### 1.3 Financial knowledge graphs

| Project | Idea | Citation |
|---|---|---|
| **FinDKG** | Time-evolving knowledge graph (issuer + event nodes) | https://xiaohui-victor-li.github.io/FinDKG/ |
| **FEEKG** | Financial-event-evolution KG, models cross-event causality | https://www.sciencedirect.com/science/article/abs/pii/S0957417424008650 |
| **FinKario** | Multi-source fusion + LLM | https://arxiv.org/abs/2508.00961 |

Common formal structure: `G(t) = (V_t, E_t)` with V_t = entity set at time t (companies,
events) and E_t = relation set (causes, owns, supplies, ...).

### 1.4 Multi-modal GNNs / Temporal Graph Learning

| Project | Idea | Citation |
|---|---|---|
| **MAGNN** | Attention-based fusion across modalities | https://www.sciencedirect.com/science/article/abs/pii/S003132032100399X |
| **MEHGT-LKG** | LLM + heterogeneous graph fusion | https://openreview.net/forum?id=N5ggpxl8Os |
| **Temporal lead-lag** | Causal-relation extraction from time-graph | https://openreview.net/forum?id=KsWRLyIAKP |

### 1.5 The seed's conclusion

> ❌ Single time-series DB is the wrong target.
> ⭕ Time-stamped multi-layer graph (TS-DB + Graph-DB + Vector-DB) is what the literature
>    converges on.

---

## §2. Identity reconciliation across data sources

The seed acknowledges many-to-many cardinality but does not address the most concrete CAQRS
blocker: **the same legal entity has different identifiers in every upstream source**, and
no one ships ground-truth mappings. This is the section the seed is missing most.

### 2.1 The reconciliation problem in CAQRS, by example

Toyota (トヨタ自動車株式会社) appears as:

| Source | Identifier | Format |
|---|---|---|
| J-Quants | `Code = "72030"` (5-digit, 4-digit ticker + check digit) | numeric string |
| yFinance | `7203.T` (4-digit + Tokyo suffix) | numeric + venue suffix |
| EDINET official | `edinetCode = "E02144"` | E + 5 digits |
| EDINET DB | same `edinet_code = "E02144"` | E + 5 digits |
| Polymarket | (not listed; would be irrelevant for this entity) | n/a |

Plus the legally-canonical Japanese identifiers:
- 法人番号 (JCN) — 13-digit corporate-registry number (same across all government services)
- 証券コード (sec_code) — same numeric as J-Quants `Code` minus the check digit

For this Issuer, J-Quants `7203` ↔ EDINET `E02144` ↔ JCN `1180301018771` are the same legal
entity, but **none of the data sources publish a cross-mapping**. CAQRS must build (or
import) the bridge.

### 2.2 Existing global identifier standards

| Standard | Issuing body | Coverage | License | Notes |
|---|---|---|---|---|
| **LEI** (Legal Entity Identifier, ISO 17442) | GLEIF | ~2.5M global entities | CC0 — public domain | 20-char alphanumeric. Mandatory under MiFID II / Dodd-Frank for derivative reporting; widely adopted by Japan-listed issuers post-2017 |
| **PermID** | LSEG / Refinitiv | ~7B records | Free for non-commercial | Includes `permID:{ric|isin|cusip}` mappings |
| **OpenFIGI** | Bloomberg | Global instrument identifiers (FIGI) | Free for non-commercial | Bidirectional with ISIN, RIC, BB ticker, CUSIP |
| **OpenCorporates** | OC ltd. | ~210M companies | Mixed (free for low-volume) | Includes JCN for Japanese entities |
| **ISIN** (ISO 6166) | Local NNAs | Per-instrument (not per-issuer) | Free lookups via national agencies | 2-letter country + 9 + check digit |
| **JCN** (法人番号) | National Tax Agency 国税庁 | All Japanese registered entities | Free, public bulk download | 13-digit; CSV master at `https://www.houjin-bangou.nta.go.jp/` |

**For CAQRS the practical anchor is**:

- **JCN** as the Japan-side canonical (it's already in EDINET responses as `JCN` field —
  see `caqrs.data.edinet.schemas.EdinetDocument.jcn`)
- **LEI** as the global canonical when available (CAQRS doesn't currently fetch this; would
  need a LEI lookup adapter)
- **OpenFIGI** for cross-listing (e.g. ADR mapping) — out of scope unless multi-venue
  arbitrage becomes a research axis

### 2.3 Reconciliation patterns from related work

| Pattern | Example | Property |
|---|---|---|
| **Probabilistic linkage** (Fellegi-Sunter) | OpenCorporates company-merger detection | Tolerant of name variations; needs labeled training |
| **Master-table + crosswalk** | Quandl / Sharadar's `tickers.csv`, OpenFIGI bulk download | Deterministic; requires periodic refresh |
| **Dual-canonical with reconciliation log** | ICIJ Offshore Leaks data graph | Records every merge decision, append-only |
| **Embedding-based linkage** | recent NLP entity-linking papers | Useful when names are heterogeneous; CAQRS won't need this for ~4k JP issuers |

CAQRS scale suggests **master-table + crosswalk**, with the LEI / JCN as canonical and a
typed `Identifier(source, value)` model for every other id we receive.

### 2.4 What the seed misses

The seed's "many-to-many" handwaving collapses two distinct problems:

- **One news item → many issuers** (a true many-to-many, requires graph edges)
- **One legal entity → many source-specific identifiers** (a one-to-many that is not
  fundamentally graph-shaped; it's a deterministic lookup table once populated)

The first is correctly modeled as `(news_id, issuer_id, salience)` edges in `G_t`. The
second is **Identifier reconciliation**, and is what the design spec in file `02` actually
formalises.

---

## §3. Implementation tech comparison

The seed names "kdb+, InfluxDB, Neo4j, FAISS" as the canonical stack. That's accurate for
hedge-fund-scale infrastructure; for a single-developer agentic-research workload (CAQRS's
target) it is dramatically over-engineered.

### 3.1 Time-series storage

| Option | Strengths | Weaknesses for CAQRS | Verdict |
|---|---|---|---|
| **kdb+ / q** | Industry-standard, columnar, blazing fast on intra-day | Proprietary licence, q-language barrier, ops-heavy | Reject |
| **InfluxDB** | Native TSDB, decent ecosystem | Wrong shape for daily OHLCV (better for high-frequency telemetry); separate process | Reject |
| **TimescaleDB** | PostgreSQL extension; SQL + hyper-tables | Postgres operational overhead for what fits in SQLite | Reject for this scale |
| **DuckDB** | Embedded analytical DB; columnar; reads Parquet natively; no server | Newer ecosystem; less mature replication story (irrelevant for single-dev) | **Adopt for backtest-scale time-series queries** |
| **Polars** (already in CAQRS) | DataFrame in-memory, lazy-evaluation | Not persistent; in-memory only | Use as the query / transform layer over DuckDB or SQLite |
| **SQLite** (already in CAQRS for caches) | Embedded, ubiquitous, no server | Not columnar (slow on wide aggregates) | Keep for cache + small reference tables; route bulk time-series via DuckDB |

### 3.2 Graph / relation storage

| Option | Strengths | Weaknesses for CAQRS | Verdict |
|---|---|---|---|
| **Neo4j** | Industry-standard graph DB, Cypher | Separate process, JVM, overkill at <100k nodes | Reject for current scale |
| **ArangoDB** | Multi-model (graph + document + KV) | Separate process, AQL learning curve | Reject |
| **TerminusDB** | Git-like graph DB, time-versioned | Niche; small community | Reject |
| **NetworkX** (Python) | In-memory graph, well-known API, no server | Not persistent; snapshot serialisation only | **Adopt as the in-process graph engine** |
| **Custom typed model on SQLite/DuckDB** | Direct adjacency table, queryable via SQL | Caller writes recursive CTEs for traversals | Adopt for relations that need persistence |

For CAQRS's scale (~3,800 EDINET DB issuers + filings + market events), an **adjacency table
in DuckDB** with a thin `caqrs.entities.graph` Python wrapper that returns NetworkX views on
demand is the right shape. No server, no extra dependency, no Cypher.

### 3.3 Vector / embedding storage

| Option | Strengths | Weaknesses for CAQRS | Verdict |
|---|---|---|---|
| **FAISS** | De facto open source standard | C++ build hassles; in-memory | Defer; we don't currently embed news |
| **Chroma / LanceDB** | Embedded, Python-native, persistent | Net-new dependency for unclear value | Defer |
| **DuckDB array_distance** + sentence-transformers | Embedded; uses existing storage | DuckDB vector ext is preview-grade as of 2026 | **Adopt** when embedding-based features land (post-current-scope) |

Since CAQRS does not currently embed news, the vector layer is a **deliberately deferred
slice**. The design spec in file `02` reserves a hole for it but does not build it.

### 3.4 The CAQRS-scale stack

```
┌─────────────────────────────────────────────────────────────────┐
│  Caller (CycleRunner, Observer, scripts/live_smoke_*)           │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  caqrs.entities — typed protocol (Issuer, Identifier, ...)      │
│    + EntityStore protocol (get_issuer_by_identifier, append_…)  │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  Implementation:  DuckDB (market series + filings + relations)  │
│                   SQLite (existing caches: yfinance, edinetdb)  │
│                   In-memory NetworkX views built on demand      │
└─────────────────────────────────────────────────────────────────┘
```

**Why both DuckDB and SQLite**: the existing caches (yFinance, EDINET DB) already use SQLite
and add no dependency. DuckDB enters as the analytical-query layer for time-aligned
multi-source reads. They co-exist without conflict (different files, different concerns).

---

## §4. LLM-agent specific patterns

CAQRS is an agent-driven research orchestrator, not a quant terminal. The integration layer
must be queryable by **typed-tool-call** (not by SQL the LLM would have to compose).

### 4.1 Reference patterns from the LLM-agent literature

| Project | Integration pattern | Take-away for CAQRS |
|---|---|---|
| **BloombergGPT** (2023) | 50B-param model trained on Bloomberg's internal corpus | Out-of-scope (CAQRS doesn't own a corpus) |
| **FinGPT** (2024) | Open-source alternative; LLM + structured-data tools (RAG) | Confirms the typed-tool-call pattern for finance |
| **LangChain SQLAgent / SQLDatabaseChain** | Agent generates SQL against a relational DB | **Anti-pattern** for CAQRS — undermines schema invariants the typed agents already rely on |
| **DSPy** finance tutorials | Composable signatures + structured output | Aligns with CAQRS's existing `Agent[I, O]` protocol |
| **AutoGen agents over Pandas** | Each tool call returns a small DataFrame | The right grain for our cycle: tool calls return tuples / lists, not raw query results |

The conclusion is the same as the existing CAQRS Agent design: **agents don't write
queries; they call typed helpers**. The data integration layer's public API must be
helper-shaped (`get_company_fundamentals(issuer_id, …)`), not query-shaped.

### 4.2 Cycle-level integration

For an Observer cycle, the integration layer needs to satisfy three call patterns:

1. **By external identifier**: "give me Issuer for J-Quants code 72030" (single-source
   lookup → `Issuer`).
2. **Multi-source projection**: "give me all price series, filings, and fundamentals for
   Issuer X over the last 2y" (cross-source temporal query → typed bundle).
3. **Relation traversal**: "what subsidiaries does Issuer X have? what Issuers filed
   大量保有報告書 about Issuer X in 2025?" (graph query → tuple of Issuers).

Patterns 1 and 2 are mostly today's data layer plus an Issuer-canonicalisation step.
Pattern 3 is the new graph layer.

### 4.3 What the seed missed about the agent layer

The seed concludes "use TS-DB + Graph-DB + Vector-DB". For an LLM-agent codebase that's a
**three-process operational footprint** for a workload that fits in SQLite + DuckDB on a
laptop. The agent gets nothing from the separation; the operator pays for it daily. Skip
it.

---

## §5. CAQRS position synthesis

| Concept (seed) | CAQRS-scale answer |
|---|---|
| Multi-modal time series `(P_t, N_t, F_t, M_t)` | We have P_t (J-Quants, yFinance), F_t (EDINET DB), partial M_t (Polymarket as event-prob proxy). N_t is deferred (no news adapter yet). |
| Financial KG `G(t) = (V_t, E_t)` | New layer: Issuer node + relations from EDINET (subsidiary, large-shareholding) + Polymarket (market about issuer) + Industry classification |
| TS-DB + Graph-DB + Vector-DB stack | DuckDB for series + SQLite (already there) for caches + in-process graph adjacency table. No separate processes. |
| Identity reconciliation | Master-table + Identifier(source, value) typed model; canonical = JCN for JP, LEI when available |
| Multi-modal GNN learning | Out of scope; we're building research-orchestration plumbing, not training models. Defer until / if a downstream slice needs it. |

**The CAQRS-shaped slice** is therefore:

1. **Issuer + Identifier model** (Issuer is canonical, Identifier maps a source-specific id
   to one Issuer)
2. **EntityStore protocol** with two implementations: in-memory (for tests) and
   DuckDB-backed (for production)
3. **Time-aligned multi-source query** — `get_market_series(issuer, kind, range)` that
   merges J-Quants + yFinance, deduplicates by source-priority, returns a typed series
4. **Filing event log** — append-only, queryable by issuer + date range + doc-type
5. **Relation graph** — adjacency table with valid_from / valid_to time bounds, exposed via
   a small typed query API (`subsidiaries_of(issuer, at=date)` etc.)
6. **Reconciliation seed loader** — periodic JCN / LEI / EDINET-code crosswalk import (the
   existing data adapters already carry the source-side identifiers we need to match)

The TyDD + TDD specification in [`02-design-spec-tydd.md`](02-design-spec-tydd.md) makes
each of those concrete.

---

## §6. Deliberately deferred

- **News / text embeddings** — no adapter yet; Vector DB layer is a hole reserved by the
  type system but not implemented.
- **Multi-modal model training** — purely research orchestration first; if a hypothesis
  asks for a trained signal extractor, that's a separate slice.
- **Cross-listing / ADR mapping** — single-venue (Tokyo + US separately) is enough for the
  current research roadmap.
- **Real-time intraday feed** — CAQRS is daily / fundamentals scale by design.

These appear in the design spec as TODO holes with explicit "deferred until X" markers, so
the type system surfaces them when a future slice needs them.
