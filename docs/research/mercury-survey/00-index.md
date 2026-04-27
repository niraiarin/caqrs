# Mercury Agent Harness — Survey Index

**Source repo**: `mercury-agent` (private prototype, `Cosmic Stack`, MIT)
**Surveyed at**: 2026-04-27
**Surveyor**: CAQRS author + Claude Opus 4.7
**Mercury commit**: HEAD of local checkout at `~/work/mercury-agent/mercury-agent-repo/` (no commit hash recorded; this is a working tree, not a pinned tag)

## Purpose

CAQRS reuses several **design ideas** from Mercury (permission gateway,
agentic loop transparency, second-brain memory). This survey establishes
what is in Mercury, what is worth porting to CAQRS, and what is
deliberately out of scope. The goal is to make the lineage decisions in
`docs/lineage.md` evidence-based rather than CLAUDE.md-summarised.

This survey covers ~4,600 lines of TypeScript across 12 files. It does
**not** copy any Mercury code — research outputs cite line ranges and
class names so the original can be re-read in context.

## Files

| # | File | Mercury source | Lines |
| --- | --- | --- | ---: |
| 01 | [overview](01-overview-from-architecture-md.md) | `ARCHITECTURE.md` | — |
| 02 | [decisions](02-decisions-from-adr.md) | `DECISIONS.md` | — |
| 03 | [agent harness](03-agent-harness.md) | `src/core/agent.ts` | 1890 |
| 04 | [permissions](04-permissions.md) | `src/capabilities/permissions.ts` | 489 |
| 05 | [capability registry](05-capability-registry.md) | `src/capabilities/registry.ts` | 191 |
| 06 | [identity / soul](06-identity-and-soul.md) | `src/soul/identity.ts` | 206 |
| 07 | [memory tiers](07-memory-tiers.md) | `src/memory/store.ts` | 206 |
| 08 | [second brain](08-second-brain.md) | `src/memory/user-memory.ts` (697) + `src/memory/second-brain-db.ts` (378) | 1075 |
| 09 | [skill progressive disclosure](09-skill-progressive-disclosure.md) | `src/skills/loader.ts` | 156 |
| 10 | [lifecycle](10-lifecycle.md) | `src/core/lifecycle.ts` | 44 |
| 11 | [scheduler](11-scheduler.md) | `src/core/scheduler.ts` | 212 |
| 12 | [providers](12-providers.md) | `src/providers/base.ts` (33) + `src/providers/registry.ts` (91) | 124 |
| 99 | [CAQRS design implications](99-caqrs-design-implications.md) | (synthesis) | — |

## Reading order

If you read top-down: 01 → 02 → 03 → 04 → ... → 12 → 99.

If you want the actionable answer first: jump to **99**.

If you only want one section, grep this index — every research file has
a `## Mercury source` link block at the top pointing back to specific
line ranges.

## Methodology

Each per-source file has the following layout:

1. `## Mercury source` — exact path + line ranges read.
2. `## Key types and entry points` — the public API surface.
3. `## Implementation patterns` — the design ideas, named.
4. `## What CAQRS already has` — current CAQRS port state (pre-survey).
5. `## CAQRS implications` — what to port, what to skip, what to extend.
6. `## Open questions` — anything ambiguous.

The synthesis (file 99) collects every `## CAQRS implications` section
into a single ranked work list.

## Status of CAQRS at survey time

- main branch: P0 + P1.0 + P1.1.a + P1.1.b + P1.1.5 + P1.1.c.1 + P1.1.c.2 (provider layer complete)
- open PR: #7 P1.1.d (expiry pre-flight + ADR-0003)
- next phase: P1.2 (Agent layer) — survey informs the design before
  starting

## License compliance

Mercury is MIT (`Cosmic Stack`). CAQRS is Apache-2.0. Research files
contain no copied source — only paraphrased descriptions, named
patterns, and line-range citations. If actual code is ever ported, it
will follow ADR-0002: SPDX header + commit hash + lineage.md entry.
