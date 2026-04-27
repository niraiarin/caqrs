# 02 — Mercury ADRs (decisions log)

## Mercury source

`DECISIONS.md` (root of Mercury repo, ~200 lines).

## ADRs that shape the harness design

### ADR-001: TypeScript + Node.js

Chose TS for the AI SDK ecosystem (Vercel AI SDK, Ink, grammY). Direct
implication for CAQRS: this is why we cannot share code — Python
financial-research stack does not cross-compile from TS (already
captured in CAQRS ADR-0001).

### ADR-005: Vercel AI SDK as the LLM seam

`generateText` / `streamText` from the `ai` package, with provider-
specific adapters. Provides:
- Unified API across providers
- Built-in streaming
- Built-in tool calling
- One-line provider swap

CAQRS analogue: P1.0/P1.1 built a hand-rolled `LLMProvider` protocol
with `httpx` because Python's `ai`-equivalent ecosystem is fragmented.
The trade-off is more code in CAQRS but tighter control over
subscription credential paths (Anthropic/Codex CLI reuse) that the
Vercel SDK does not surface.

### ADR-006: Soul as four markdown files

- **Decision**: `soul.md`, `persona.md`, `taste.md`, `heartbeat.md`.
  Only `soul + persona` injected every request; `taste + heartbeat`
  selectively.
- **Rationale**: ~350 token baseline for identity. Owner can edit
  personality without code changes.
- **CAQRS posture**: skipped. CAQRS agents are typed pure functions
  (Observer, Hypothesis, Skeptic, ...) — they have **roles**, not
  **personas**. The system prompt for each agent is a small role
  description plus its input schema, not a layered identity.

### ADR-007: Agent Skills spec

- **Decision**: `SKILL.md` with YAML frontmatter; progressive
  disclosure (name+description at startup, body on demand);
  installable from URL or pasted content.
- **Rationale**: token-efficient extension without code changes.
- **CAQRS posture**: deferred. CAQRS agents are statically registered.
  However, **research playbooks** (e.g., "12-1 momentum on US large-
  cap", "earnings drift on small-cap") could later use a similar
  progressive-disclosure pattern: a catalog of strategy templates
  loaded by name + description, with full body fetched only when the
  Hypothesis Agent picks one to investigate.

### ADR-008: Scheduler as AI-callable tools

- **Decision**: `schedule_task` / `list_scheduled_tasks` /
  `cancel_scheduled_task` as tools the LLM can call. Persistence in
  `~/.mercury/schedules.yaml`. Tasks fire as internal messages.
- **Rationale**: Mercury can autonomously schedule its own work.
- **CAQRS posture**: probably skipped at the agent level. CAQRS
  research cycles are launched explicitly (`run cycle <hypothesis-id>`
  or via cron at the OS level), not autonomously by an agent. If a
  daemon mode is added later (P4+), this pattern can be lifted.

### ADR-009: Daemonization

Hybrid approach with `child_process.spawn({detached: true})` + watchdog
+ platform service generators (systemd / LaunchAgent / Task Scheduler).
Crash recovery with exponential backoff (1s, 1.25x, max 10/60s).

CAQRS posture: not in P1. Research prototype runs in foreground. If
daemonization becomes necessary later, adopt the same three-layer
pattern (spawn → watchdog → service installer).

### ADR-010: Second Brain — SQLite + business logic

- **Decision**: SQLite (better-sqlite3) for storage, `UserMemoryStore`
  for business logic, autonomous merge / conflict / tier / decay,
  fire-and-forget extraction after each response, FTS5 for search.
- **Key principle**: **autonomous, no review queue, no manual
  pinning**. Memories are stored, merged, and de-conflicted by
  confidence score. Weak memories survive with low scores and decay.
- **10 memory types**: `identity, preference, goal, project, habit,
  decision, constraint, relationship, episode, reflection`.
- **Tiers**: `active` (time-bound) vs `durable` (long-lived). Memory
  reinforced 3+ times is promoted from active to durable.
- **Staleness**: active inferred memory not seen in 21 days is
  dismissed. Durable inferred memory with no reinforcement in 120 days
  has confidence decayed; below 0.3 it is dismissed.
- **CAQRS posture**: directly adopt the autonomous-memory pattern,
  but with **research-domain memory types**:
  - hypothesis (active until adopted/rejected)
  - regime (active, time-windowed)
  - failed_hypothesis (durable, becomes negative training signal)
  - accepted_strategy (durable)
  - lesson (reflection-equivalent: e.g., "AAPL earnings drift fails
    when VIX > 30")

The **autonomy principle** transfers exactly: research artifacts merge
on overlap, conflict on opposite findings, tier by reinforcement count,
decay by staleness. The user does not curate this manually.

## ADRs not directly relevant to harness design

- ADR-002 (Ink for TUI) — UI concern, CAQRS has no Ink
- ADR-003 (flat-file memory) — superseded by ADR-010
- ADR-004 (grammY for Telegram) — channel concern, CAQRS skips

## CAQRS implications

| Mercury ADR | CAQRS posture |
| --- | --- |
| ADR-005 (Vercel AI SDK) | Hand-rolled `LLMProvider` for subscription paths, accept the cost |
| ADR-006 (4-file soul) | Skip — agents have roles, not personas |
| ADR-007 (Agent Skills) | Defer; revisit for research-playbook catalog (P2+) |
| ADR-008 (scheduler tools) | Skip in P1; OS-level cron for now |
| ADR-009 (daemonization) | Skip in P1; pattern is reusable later |
| ADR-010 (second brain) | **Adopt directly**, swap memory types for research-domain (P1.3) |

## Open questions

- Mercury's ADR template is one-decision-per-section without status
  tracking ("Accepted / Superseded / Reconsidered"). CAQRS already
  uses richer ADRs (ADR-0001 has explicit `Status`, `Context`,
  `Decision`, `Consequences`, `Alternatives rejected`,
  `Reconsider when`). No change needed; CAQRS's format is strictly
  better for evolving research projects.
