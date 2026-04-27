# 01 — Mercury Architecture Overview

## Mercury source

`ARCHITECTURE.md` (root of Mercury repo, ~300 lines).

## Key claims (from the doc)

- **Soul-driven, token-efficient AI agent that runs 24/7**.
- An **orchestrator**, not just a chatbot; reads/writes files, runs
  commands, performs multi-step agentic workflows, all governed by a
  strict permission system.
- Channel-agnostic core: same brain, multiple I/O surfaces (CLI,
  Telegram; future: Signal, Discord, Slack).
- Persistent memory across restarts.

## The four "human analogy" layers

| Mercury concept | Human analogy | File |
| --- | --- | --- |
| `soul.md` | Heart | `soul/soul.md` |
| `persona.md` | Face | `soul/persona.md` |
| `taste.md` | Palate | `soul/taste.md` |
| `heartbeat.md` | Breathing | `soul/heartbeat.md` |
| Short-term memory | Working memory | `src/memory/store.ts` |
| Episodic memory | Recent experiences | `src/memory/store.ts` |
| Long-term memory | Life lessons | `src/memory/store.ts` |
| Second brain | Structured user model | `src/memory/user-memory.ts` + `second-brain-db.ts` |
| Providers | Senses | `src/providers/` |
| Capabilities | Hands & tools | `src/capabilities/` |
| Permissions | Boundaries | `src/capabilities/permissions.ts` |
| Channels | Communication | `src/channels/` |
| Heartbeat / scheduler | Circadian rhythm | `src/core/scheduler.ts` |
| Lifecycle | Awake / sleep / think | `src/core/lifecycle.ts` |

## The agentic loop (Mercury's own words)

```
User message → Agent loads system prompt (soul + guardrails + persona)
  → Agent calls generateText({ tools, maxSteps: 10 })
    → LLM decides: respond with text OR call a tool
      → If tool called:
        → Permission check (filesystem scope / shell blocklist)
        → If allowed: execute tool, return result to LLM
        → If denied: LLM gets denial message, adjusts approach
        → LLM continues (next step) — may call more tools or respond
      → If text: final response returned to user
  → Agent sends final response via channel
```

`maxSteps: 10` is the **soft step cap**; the harder cap (25 absolute,
12 failed) lives in `ToolCallLoopDetector` (see file 03).

## Permission model summary

- **Filesystem**: folder-level scope (`{path, read, write}`). Paths
  without scope = no access, must ask user. User answers
  `y` / `always` / `n`. `always` saves to `~/.mercury/permissions.yaml`.
- **Shell**: blocklist (never executed) + auto-approve list (no prompt)
  + needs-approval list. Commands restricted to CWD + approved scopes.
- **Inline UX**: when scope is missing, the agent prompts inside the
  current channel rather than failing.

## Token budget engineering

- System prompt baseline (soul + guardrails + persona): **~500 tokens
  per request**.
- Short-term context: last 10 messages.
- Long-term facts: keyword-matched, ~3 facts injected.
- Second brain: relevant memories injected via `retrieveRelevant()`,
  capped at ~900 chars.
- Daily default cap: 1,000,000 tokens.

## Second brain summary

Already detailed in ADR-010 (file 02) and the user-memory survey (file
08). Key claim: **autonomous, no review queue**, runs as fire-and-forget
after each non-trivial response, ~800 tokens per extraction call.

10 memory types: `identity, preference, goal, project, habit, decision,
constraint, relationship, episode, reflection`.

## Skills (Agent Skills spec)

- Each skill = directory under `~/.mercury/skills/` containing
  `SKILL.md` (YAML frontmatter + markdown).
- **Progressive disclosure**: at startup only the skill name +
  description are loaded (token-efficient). Full skill body loaded on
  invocation via `use_skill` tool.
- `install_skill`, `list_skills`, `use_skill` are AI-callable.

## Scheduler

- Cron expressions stored in `~/.mercury/schedules.yaml`, restored on
  startup.
- Scheduled tasks fire as **internal messages** through the agent loop
  — they do not produce visible channel output unless the agent
  explicitly emits.
- Tools: `schedule_task`, `list_scheduled_tasks`,
  `cancel_scheduled_task`.

## Runtime data location

All persistent state under `~/.mercury/` (not in the project dir):

| What | Path |
| --- | --- |
| Config | `~/.mercury/mercury.yaml` |
| Soul files | `~/.mercury/soul/*.md` |
| Memory | `~/.mercury/memory/` |
| Skills | `~/.mercury/skills/` |
| Schedules | `~/.mercury/schedules.yaml` |
| Permissions | `~/.mercury/permissions.yaml` |
| Daemon pid + log | `~/.mercury/` |
| Second brain SQLite | `~/.mercury/memory/second-brain/` |

CAQRS analogue: `~/.caqrs/` (not yet implemented; will need an
equivalent `getCaqrsHome()` helper in the orchestrator).

## CAQRS implications

- **Adopt**: orchestrator + channel-agnostic core split, `~/.mercury/`
  ↔ `~/.caqrs/` runtime layout, persistent memory across restarts,
  token budget engineering at the prompt-build seam.
- **Invert**: Mercury has **one big agent** that calls many tools;
  CAQRS has **many small typed agents** in a pipeline. The `maxSteps:
  10` cap will be replaced by a per-agent budget enforced by the
  orchestrator boundary.
- **Skip**: persona / taste / heartbeat soul files (CAQRS agents are
  typed pure functions, not personas).
- **Skip for now**: channel multiplexing beyond CLI; CAQRS is single-user
  research-prototype scoped.

## Open questions

- Mercury caches the loaded soul files in memory (`Identity.cache`).
  Does CAQRS need an equivalent caching layer for its system-prompt
  baseline, or is per-call composition cheap enough? (probably the
  latter — to be decided in P1.2.)
- Mercury's `~/.mercury/` directory is owner-controlled state. CAQRS
  needs to decide what is project-local (in repo) vs user-local
  (`~/.caqrs/`). For artifacts (hypothesis cards, backtest reports)
  the answer is probably project-local; for credentials, user-local.
