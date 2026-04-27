# ADR-0001: CAQRS lives in an independent repository, not as a Mercury fork

- **Status**: Accepted
- **Date**: 2026-04-27

## Context

CAQRS and Mercury share architectural ideas (permission gateway, agentic
loop state machine, artifact-based design, second-brain memory). A
reasonable default would have been to fork Mercury and add financial-research
capabilities as a sub-module.

## Decision

Build CAQRS as an independent Python repository. Reuse Mercury's *design
ideas* through `docs/lineage.md`; do not share code.

## Consequences

### Positive

- No upstream-tracking debt. Mercury continues to evolve as a personal
  assistant with channel-agnostic UI; CAQRS does not need that surface.
- Native Python access to the financial-research stack (vectorbt, pandas,
  statsmodels, yfinance, FRED, PRAW). Cross-compilation from TypeScript is
  not realistic at any reasonable cost.
- Channel separation. A research orchestrator should not be coupled to chat
  transports. Telegram-as-dashboard becomes a *consumer* of CAQRS artifacts,
  not part of CAQRS itself.

### Negative

- Some code duplication (provider fallback, token budgeting, run-metadata
  utilities). Tracked in `docs/lineage.md`. Will be addressed *only* if the
  duplication grows to ≥ 3 distinct features rather than preemptively
  factored.

### Mitigation

- After P3, evaluate whether a Mercury skill `caqrs-watch` should consume
  CAQRS artifacts (Mercury-as-dashboard pattern). This is an integration,
  not a merge.

## Alternatives rejected

- **Fork Mercury**: rejected for the language-stack mismatch above.
- **Submodule under Mercury**: rejected because the mixed TS/Python build
  is a packaging burden out of proportion to the duplication saved.
- **Shared monorepo with TS + Python sub-projects**: rejected because the
  audience for a research orchestrator does not overlap with a personal
  assistant; the cost of monorepo tooling pays for itself only when shared
  consumers exist.
