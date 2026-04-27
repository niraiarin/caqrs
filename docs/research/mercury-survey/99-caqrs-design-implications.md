# 99 — CAQRS Design Implications (Synthesis)

This file synthesises the per-source surveys (01-12) into a single
actionable work list. Each entry names a Mercury pattern, its CAQRS
target module, the proposed phase, and the level of code reuse vs
rewrite.

## Adoption matrix

Legend:

- **Adopt fully**: port the pattern with structural fidelity. The
  Python module will look line-for-line analogous to the TS source.
- **Adopt selectively**: port the design idea, rewrite for the
  research domain. Names and types change; the algorithm transfers.
- **Invert / adapt**: take the inverse of Mercury's choice (e.g.,
  one-big-agent vs many-small-agents).
- **Skip**: not applicable to a research orchestrator.

| # | Mercury pattern (source file) | CAQRS module (target) | Phase | Reuse posture |
| --- | --- | --- | --- | --- |
| 1 | ToolCallLoopDetector (`agent.ts:34-240`) | `caqrs/orchestrator/loop_detector.py` | **P1.2** | Adopt fully |
| 2 | Pre-LLM warning injection (`agent.ts:414-458`) | `caqrs/orchestrator/preflight.py` | **P1.2** | Adopt fully |
| 3 | Provider fallback iterator + last-success bias (`providers/registry.ts`) | `caqrs/providers/registry.py` (already done in P1.0) | done | Adopted; CAQRS richer |
| 4 | onStepFinish hot path (`agent.ts:534-657, 699-823`) | replaced by typed agent results — no analogue needed | n/a | Adapt — typing replaces string-based failure detection |
| 5 | use_skill loop-detector reset (`agent.ts:559-561`) | sub-agent reset hook in orchestrator | **P1.2** | Adopt selectively |
| 6 | auto-approve-all for internal tasks (`agent.ts:336-341`, permissions.ts:176-182) | orchestrator headless-mode flag | **P1.2** | Adopt selectively |
| 7 | askToContinue second-chance (`agent.ts:588-600`) | optional human-in-the-loop hook | **P1.2** (later) | Adopt selectively |
| 8 | Memory injection at prompt-build (`agent.ts:460-487`, `user-memory.ts:116-168`) | `caqrs/memory/retrieve.py` + agent prompt builder | **P1.3** | Adopt fully |
| 9 | buildSystemPrompt composition (`agent.ts:925-981`) | per-agent role prompt builder | **P1.2** | Invert — minimal per-agent role, no soul |
| 10 | extractMemory background fire-and-forget (`agent.ts:1080-1163`) | `caqrs/memory/extractor.py` | **P1.3** | Adopt fully |
| 11 | heartbeat proactive maintenance (`agent.ts:1024-1078`) | `caqrs/orchestrator/heartbeat.py` | **P1.4** (or later) | Adopt fully |
| 12 | processInternalPrompt self-fire (`agent.ts:983-993`) | `Orchestrator.fire_internal_cycle` | **P1.4** | Adopt fully |
| 13 | Sequential queue with reentrancy guard (`agent.ts:280-303`) | `caqrs/orchestrator/queue.py` | **P1.4** | Adopt fully |
| 14 | Token budget at entry edge (`agent.ts:393-412`) | `caqrs/orchestrator/budget.py` | **P1.4** | Adopt fully |
| 15 | Channel-aware streaming (`agent.ts:510-679`) | skip — research outputs are atomic artifacts | n/a | Skip |
| 16 | Per-tool feedback events (`agent.ts:601-632, 766-797`) | structured event log emitter | **P1.4** | Adopt selectively |
| 17 | Three-tier shell rules (`permissions.ts:41-144`) | `caqrs/policy/rules/{universe,size,leverage,action_class}.py` | **P3** | Adopt selectively — same shape, different domain |
| 18 | Inline yes/always/no UX (`permissions.ts:172-174, 402-421`) | `caqrs/policy/approval_handler.py` | **P3** | Adopt fully |
| 19 | Skill / playbook elevation (`permissions.ts:184-207`) | per-playbook scope widening | P2+ | Adopt fully |
| 20 | Temporary scopes (`permissions.ts:423-437`) | per-cycle policy relaxation | **P3** | Adopt fully |
| 21 | Capability conditional registration (`registry.ts:106-178`) | `caqrs/tools/registry.py` | **P1.2** | Adopt fully |
| 22 | ChatCommandContext bridge (`registry.ts:40-50`) | `OrchestratorIntrospection` struct | **P1.4** | Adopt selectively |
| 23 | Per-conversation short-term cap (`memory/store.ts:56-107`) | per-cycle artifact buffer | **P1.3** | Adopt fully |
| 24 | Long-term keyword search (`memory/store.ts:109-155`) | small SQLite store for research constants | **P1.3** | Adopt selectively — use FTS5 from start |
| 25 | Episodic prune-by-age (`memory/store.ts:181-190`) | `caqrs/memory/episodic.py` | **P1.3** | Adopt fully — 30-day default vs Mercury 7-day |
| 26 | Second brain SQLite + FTS5 schema (`second-brain-db.ts`) | `caqrs/memory/second_brain/db.py` | **P1.3** | Adopt fully |
| 27 | Optional dependency probe (`second-brain-db.ts:11-29`) | not needed (sqlite3 in stdlib) | n/a | Skip |
| 28 | Soft-delete via dismissed flag (`second-brain-db.ts`) | same | **P1.3** | Adopt fully |
| 29 | Meta key/value table (`second-brain-db.ts:107-110, 344-358`) | same — derived data separate from primary | **P1.3** | Adopt fully |
| 30 | shouldStoreCandidate filter (`user-memory.ts:470-476`) | `caqrs/memory/second_brain/candidate.py` | **P1.3** | Adopt selectively — research-domain thresholds |
| 31 | remember() merge/conflict pipeline (`user-memory.ts:170-203`) | `caqrs/memory/second_brain/store.py` | **P1.3** | Adopt fully |
| 32 | resolveConflict (higher confidence wins; equal → newer) (`user-memory.ts:356-393`) | `caqrs/memory/second_brain/conflict.py` | **P1.3** | Adopt fully |
| 33 | enforceMaxRecords with health-score eviction (`user-memory.ts:395-411`) | same | **P1.3** | Adopt fully |
| 34 | memoryHealthScore weighted sum (`user-memory.ts:489-497`) | same | **P1.3** | Adopt fully |
| 35 | effectiveConfidence — age + evidence-kind decay (`user-memory.ts:499-516`) | same | **P1.3** | Adopt fully |
| 36 | isRowStale — scope-specific cutoffs (`user-memory.ts:518-527`) | same — research-domain cutoffs | **P1.3** | Adopt selectively |
| 37 | retrieveRelevant — FTS5 + scoreAndRank + maxChars cap (`user-memory.ts:116-168`) | same | **P1.3** | Adopt fully |
| 38 | consolidate() periodic re-synthesis (`user-memory.ts:217-260`) | `caqrs/memory/second_brain/consolidate.py` | **P1.3** | Adopt selectively — research reflection types |
| 39 | hasConflict — polarity detection (`user-memory.ts:572-604`) | research-domain polarity pairs | **P1.3** | Adopt selectively |
| 40 | Skill progressive disclosure (`skills/loader.ts`) | research playbook catalog | **P2+** | Adopt fully (later) |
| 41 | Lifecycle whitelist transitions (`core/lifecycle.ts`) | `caqrs/orchestrator/state_machine.py` | **P1.2** or **P1.4** | Adopt fully |
| 42 | Scheduler cron + delayed + heartbeat (`core/scheduler.ts`) | `caqrs/orchestrator/scheduler.py` | **P1.4** | Adopt selectively |
| 43 | Identity 4-file soul / persona / taste / heartbeat (`soul/identity.ts`) | not used | n/a | Skip |
| 44 | Hardcoded GUARDRAILS block (`soul/identity.ts:103-115`) | research guardrails as Python constant | **P1.2** | Adopt selectively |

**Total entries**: 44. **Adopt fully**: 22. **Adopt selectively**:
13. **Invert / adapt**: 2. **Skip**: 7.

## Phase plan (revised after survey)

### P1.2 — Agent layer foundation

Modules to create:

```
caqrs/agents/
├── observer.py
├── hypothesis.py
├── skeptic.py
├── research.py
├── auditor.py
├── prompts/
│   ├── guardrails.py        # research GUARDRAILS constant (#44)
│   └── role_template.py     # per-agent prompt builder (#9, inverted)
└── (protocol.py from P1.0)

caqrs/orchestrator/
├── loop_detector.py         # ToolCallLoopDetector port (#1)
├── preflight.py             # pre-LLM warning injection (#2)
├── state_machine.py         # lifecycle (#41)
└── headless_mode.py         # auto-approve-all flag (#6)

caqrs/tools/
├── registry.py              # ToolRegistry (#21)
├── base.py                  # Tool protocol
└── meta/
    └── emit_artifact.py     # emit_<schema>(...) factory
```

Tests cover loop detector, preflight, state machine, registry, and
the agent protocol shape.

P1.2.5 (optional): research playbook catalog (#40), defer if not
needed.

### P1.3 — Memory layer

Modules to create:

```
caqrs/memory/
├── short_term.py            # per-cycle artifact buffer (#23)
├── episodic.py              # JSONL log + 30-day prune (#25)
├── long_term.py             # SQLite small-table store (#24)
├── retrieve.py              # cross-tier retrieval (#8, #37)
└── second_brain/
    ├── db.py                # SQLite + FTS5 schema (#26, #28, #29)
    ├── store.py             # ResearchMemoryStore (#30, #31)
    ├── merge.py             # mergeRecord + pickBetterSummary (#31)
    ├── conflict.py          # resolveConflict + hasConflict (#32, #39)
    ├── score.py             # memoryHealthScore + effectiveConfidence (#34, #35)
    ├── stale.py             # isRowStale + cutoffs (#36)
    ├── consolidate.py       # buildProfile/Active/Reflection (#38)
    └── extractor.py         # background memory extraction (#10)
```

This is a substantial subsystem (~1000 Python lines). Build
incrementally per the priority sequence in file 08.

### P1.4 — Orchestrator wiring

```
caqrs/orchestrator/
├── orchestrator.py          # main entry (sequential queue + lifecycle)
├── queue.py                 # cycle queue with reentrancy guard (#13)
├── budget.py                # per-experiment budget (#14)
├── heartbeat.py             # consolidate + prune + notify (#11)
├── scheduler.py             # cron + delayed (#42, simplified)
├── event_log.py             # structured events (#16)
├── introspection.py         # IntrospectionContext (#22)
└── self_fire.py             # internal cycle fire (#12)
```

This phase wires everything together. Most code is glue; the
sub-modules from P1.2 and P1.3 do the work.

### P3 — Policy Gateway (after research loop is closed)

```
caqrs/policy/
├── manifest.py              # YAML/TOML schema for policy
├── gateway.py               # main entry (#17, #18)
├── rules/
│   ├── universe.py
│   ├── size.py
│   ├── concentration.py
│   ├── leverage.py
│   └── action_class.py
├── elevation.py             # per-playbook elevation (#19)
├── temp_scope.py            # session-only relaxation (#20)
└── approval_handler.py      # CLI / Slack / autorun (#18)
```

## Cross-cutting decisions

### A. CAQRS agents are typed pure functions, not personas

Mercury has **one big Agent** with persona, lifecycle, memory, and
multi-tool access all bundled. CAQRS has **many small Agents**, each
typed (`Agent[I, O]`), each with a narrow role. The orchestrator
sequences them.

This shapes everything: identity / soul → not adopted; capability
registry → still adopted but each agent gets a filtered subset; loop
detection → still adopted but per-agent budget instead of per-message
budget.

### B. Typed tool results replace string-based failure heuristics

Mercury detects tool failure by string matching (`"Error:" /
"exited with code" / "Command failed"`). CAQRS gets this for free
because tool results are typed pydantic models — `failed: bool` (or
specific error subclasses) lives on the result type. This is a
**direct correctness improvement** over Mercury.

### C. Mercury's negotiation-with-LLM vs CAQRS's hard-limits

Mercury injects `[SYSTEM WARNING]` as an in-context message to the
LLM, hoping the model will adjust. CAQRS imposes hard limits at the
orchestrator boundary — agents do not see warning messages, the
orchestrator simply aborts.

This is more deterministic and safer for a research workload but
loses Mercury's ability to recover from a non-pathological loop
(e.g., legitimate exploration that looks like a loop).

### D. Mercury reads research-domain "personality" CAQRS reads research artifacts

Mercury's memory injects user identity / preferences. CAQRS injects
prior research state (regime fingerprints, accepted strategies,
known data-quality issues). The retrieval mechanism is the same
(FTS5 + scoreAndRank + maxChars budget); the *content* differs.

### E. CAQRS does not need channel multiplexing

Mercury's CLI ⇄ Telegram parity, streaming flag, channel-aware
permission UX, are all valuable for a personal-assistant agent. CAQRS
runs in a single mode (CLI for research operator, log files for
audit). Telegram-style integrations come later as artifact consumers,
not as input channels.

## Lineage.md update proposal

The current `docs/lineage.md` table has 6 Mercury imports. After this
survey, the proper table has **44 entries**. To keep lineage.md
readable, propose **collapsing** by subsystem:

```markdown
## Imported from Mercury (MIT, by Cosmic Stack)

| Mercury subsystem | CAQRS port location | Adoption posture |
| --- | --- | --- |
| Agent harness loop detection (`src/core/agent.ts`, ToolCallLoopDetector + pre-LLM warning) | `caqrs/orchestrator/{loop_detector,preflight}.py` | full |
| Provider fallback registry pattern (`src/providers/registry.ts`) | `caqrs/providers/registry.py` (P1.0) | full |
| Permission gateway (filesystem scope + shell rules + inline UX) (`src/capabilities/permissions.ts`) | `caqrs/policy/` (P3) | selective |
| Capability registry pattern (`src/capabilities/registry.ts`) | `caqrs/tools/registry.py` | full |
| Memory tiers — short / long / episodic (`src/memory/store.ts`) | `caqrs/memory/{short_term,long_term,episodic}.py` | full |
| Second brain — SQLite + FTS5 + autonomous merge / conflict / decay (`src/memory/{user-memory,second-brain-db}.ts`) | `caqrs/memory/second_brain/` | full |
| Lifecycle whitelist state machine (`src/core/lifecycle.ts`) | `caqrs/orchestrator/state_machine.py` | full |
| Scheduler cron + delayed + heartbeat (`src/core/scheduler.ts`) | `caqrs/orchestrator/scheduler.py` (P1.4) | selective |
| Skill / playbook progressive disclosure (`src/skills/loader.ts`) | `caqrs/playbooks/loader.py` (P2+) | full |
| Token budget at the entry edge (`src/utils/tokens.ts`, used by agent.ts) | `caqrs/orchestrator/budget.py` | full |
| Background memory extraction (fire-and-forget) (`src/core/agent.ts:extractMemory`) | `caqrs/memory/second_brain/extractor.py` | full |

## Deliberately not imported

- Soul / persona / taste / heartbeat (`src/soul/`) — CAQRS agents have roles, not personas
- Channel multiplexing (`src/channels/`) — research orchestrator runs in single mode
- Daemonization + watchdog (`src/cli/`) — research prototype runs in foreground
- Telegram integration (`src/channels/telegram.ts`) — out of scope
- TUI (Ink) — CLI suffices for research operator
- Vercel AI SDK as transport seam — CAQRS uses httpx directly to control subscription credential paths

## Detail audit

The full per-pattern audit lives in `docs/research/mercury-survey/`.
Every pattern in this lineage table is cited with line ranges in the
corresponding research file. If the Mercury source moves or refactors,
the research files document what the imported design *was* at the
time of port.
```

This collapsed view is what `docs/lineage.md` should look like after
the survey is merged. The detailed evidence stays in the research
files; the index points to them.

## Open questions remaining

1. **Should the Mercury repo be added as a git submodule or pinned
   dep?** — Considered no (cf. ADR-0001). The research files cite
   line numbers which become stale; revisit if Mercury source
   refactors substantially.
2. **Mercury's `consolidate()` runs at 5-minute throttle. CAQRS
   research cycles run at minutes-to-hours cadence.** — Likely
   adjust to 30-60 min throttle, or trigger only post-cycle.
3. **Mercury health score weights are empirically tuned** — does
   CAQRS keep them (importance 0.35, durability 0.25, ...) as
   defaults or tune for research domain? Defer; start with Mercury's
   defaults, instrument, retune after first 100 cycles.
4. **Per-agent vs per-cycle budget** — Mercury has per-message
   budget (with daily cap). CAQRS could have per-agent (each call
   capped) AND per-cycle (full pipeline capped). Recommend both,
   with per-cycle being the operationally critical one.

## Survey complete

- **Read**: 4,593 lines of TypeScript across 12 Mercury files.
- **Wrote**: 13 research files (00-index + 01-12 + 99-synthesis) =
  3,654 markdown lines (this file inclusive).
- **Compression ratio**: ~1.25x — the research files are slightly
  longer than the source they describe because each pattern gets
  named, contextualised, and translated into CAQRS implications.

The survey is no longer the bottleneck on P1.2 design.
