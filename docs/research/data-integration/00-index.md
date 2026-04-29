# Data Integration — Survey + Design Index

**Surveyed at**: 2026-04-29
**Surveyor**: CAQRS author + Claude Opus 4.7
**CAQRS revision at survey time**: `ddef11a` (P3.d-3 + observer helpers complete; data layer
covers J-Quants, yFinance, Polymarket CLOB / Gamma / archive, EDINET official, EDINET DB)

## Why this survey

CAQRS now fetches from 5 distinct upstream services. The next problem is **how to organise the
collected data so downstream agents can act on it**. The user's seed research (file `01-survey.md`,
section "User-supplied prior art") concludes that the right shape is a "time-stamped multi-layer
graph", not a single unified time series. This survey extends that seed in four directions
the seed touches only obliquely:

1. **Identity reconciliation across sources** — same Toyota appears as `7203` (J-Quants),
   `E02144` (EDINET / EDINET DB), `7203.T` (yFinance), `1301` (varies in Polymarket if used);
   without a canonical-issuer model, all the multi-modal work below falls apart.
2. **Open-source implementation tech** — what storage / query stacks actually scale to a
   single-developer agentic-research workload (vs. Bloomberg-class infrastructure).
3. **LLM-agent specific patterns** — how RAG / tool-use frameworks consume financial data
   stores, and which integration pattern fits CAQRS's CycleRunner.
4. **CAQRS position** — synthesis of what this codebase should build, what to outsource, and
   what to deliberately defer.

The output is **two documents**: a survey (this directory) plus a TyDD + TDD-driven design
specification that the survey's conclusions feed into.

## Files

| # | File | Scope |
|---|---|---|
| 00 | (this file) | Survey index |
| 01 | [survey](01-survey.md) | Multi-modal time series + financial KGs + identity reconciliation + impl tech + LLM patterns |
| 02 | [design-spec-tydd](02-design-spec-tydd.md) | TyDD + TDD-driven design specification for the CAQRS data integration layer |

## Reading order

- Top-down (research-first): `00 → 01 → 02`
- Top-down (design-first, you trust the survey conclusions): `00 → 02`
- Diff-driven (what's new vs the user's seed): jump to `01 §"Identity reconciliation"` and
  `01 §"Implementation tech comparison"` — those two sections are the substantive additions

## Methodology

Each survey section follows:

1. **Prior art** — referenced papers / projects with concrete citations
2. **CAQRS-specific problem** — how the abstract concept maps onto this codebase
3. **Position** — what we should adopt, adapt, or defer

The design spec (file `02`) follows the TyDD + TDD discipline from `~/.claude/CLAUDE.md`:

1. **Test list** — declarative verifiable goals derived from acceptance criteria
2. **Type contract** — public APIs, frozen Pydantic schemas, holes the type checker enforces
3. **Failing examples first** — Given-When-Then scenarios per test list entry
4. **Implementation guidance** — minimum work to flip red → green
5. **Property-based tests** — round-trip / commutativity / temporal invariants

## Status of the data layer at survey time

- main: `ddef11a` (P3.d-3 + observer helpers)
- pytest: 736 passed, 9 deselected; ruff / mypy clean; CI 4 jobs green
- Data sources fully wrapped: J-Quants V2, yFinance (production-aligned), Polymarket CLOB +
  Gamma + archive, EDINET official v2, EDINET DB v1 (with cache + 100 req/day quota tracker)
- Cross-source primitives: `caqrs.data._common.AsyncRateLimiter` shared across all clients
- Helpers: `fetch_jquants_asset_snapshot`, `fetch_yfinance_asset_snapshot`,
  `fetch_polymarket_signal`, `fetch_recent_filings` (EDINET official),
  `fetch_edinetdb_company_fundamentals`
- **Missing layer (this survey's target)**: no canonical Issuer model, no cross-source
  identifier reconciliation, no time-aligned multi-source query, no event log, no relation
  graph
