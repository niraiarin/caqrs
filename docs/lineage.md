# Design lineage: from Mercury and OpenClaw to CAQRS

CAQRS is **not** a fork of Mercury or OpenClaw. However, several
design principles are imported. This document records what was
borrowed, what was deliberately changed, and what was skipped.

The detailed evidence base for the Mercury imports below lives at
`docs/research/mercury-survey/` (13 files surveying ~4,600 lines of
Mercury source).

## Imported from Mercury (MIT, by Cosmic Stack)

| Mercury subsystem (cited research file) | CAQRS port location | Posture |
| --- | --- | --- |
| Agent harness loop detection — `ToolCallLoopDetector` + pre-LLM warning injection (`src/core/agent.ts:34-240, 414-458`) — see [03](research/mercury-survey/03-agent-harness.md) | `caqrs/orchestrator/{loop_detector,preflight}.py` | full |
| Provider fallback registry — last-success bias, ordered iterator (`src/providers/registry.ts`) — see [12](research/mercury-survey/12-providers.md) | `caqrs/providers/registry.py` (P1.0) | full |
| Permission gateway — three-tier rules + inline yes/always/no UX + temp scope + skill elevation (`src/capabilities/permissions.ts`) — see [04](research/mercury-survey/04-permissions.md) | `caqrs/policy/` (P3) | selective — research-domain rules |
| Capability registry — conditional registration + closure-bound context (`src/capabilities/registry.ts`) — see [05](research/mercury-survey/05-capability-registry.md) | `caqrs/tools/registry.py` (P1.2) | full |
| Memory tiers — short / long / episodic with prune (`src/memory/store.ts`) — see [07](research/mercury-survey/07-memory-tiers.md) | `caqrs/memory/{short_term,long_term,episodic}.py` (P1.3) | full |
| Second brain — SQLite + FTS5 + autonomous merge / conflict / decay / consolidation (`src/memory/{user-memory,second-brain-db}.ts`) — see [08](research/mercury-survey/08-second-brain.md) | `caqrs/memory/second_brain/` (P1.3) | full |
| Lifecycle whitelist state machine (`src/core/lifecycle.ts`) — see [10](research/mercury-survey/10-lifecycle.md) | `caqrs/orchestrator/state_machine.py` (P1.2 or P1.4) | full |
| Scheduler — cron + delayed + heartbeat (`src/core/scheduler.ts`) — see [11](research/mercury-survey/11-scheduler.md) | `caqrs/orchestrator/scheduler.py` (P1.4) | selective — Python `croniter`, no node-cron |
| Skill / playbook progressive disclosure (`src/skills/loader.ts`) — see [09](research/mercury-survey/09-skill-progressive-disclosure.md) | `caqrs/playbooks/loader.py` (P2+) | full (deferred) |
| Token budget at the entry edge — runtime override + over-budget short-circuit (`src/core/agent.ts:393-412`) — see [03](research/mercury-survey/03-agent-harness.md) | `caqrs/orchestrator/budget.py` (P1.4) | full |
| Background memory extraction — fire-and-forget LLM call after each response (`src/core/agent.ts:1080-1163`) — see [03](research/mercury-survey/03-agent-harness.md) | `caqrs/memory/second_brain/extractor.py` (P1.3) | full |
| Hardcoded GUARDRAILS block injected into system prompt (`src/soul/identity.ts:103-115`) — see [06](research/mercury-survey/06-identity-and-soul.md) | `caqrs/agents/prompts/guardrails.py` (P1.2) | selective — research guardrails (no leverage, cite sources, walk-forward required) |

## Deliberately changed (Mercury → CAQRS)

- **Language**: TypeScript → Python. The financial-research stack
  (vectorbt, pandas, statsmodels, yfinance, FRED, PRAW) does not
  cross-compile.
- **Agent shape**: Mercury has **one big Agent** with persona,
  multi-tool access, and conversational state. CAQRS has **many
  small Agents**, each typed (`Agent[I, O]`), each with a narrow
  role. The orchestrator sequences them.
- **Tool result typing**: Mercury detects tool failure by string
  matching (`"Error:"`, `"exited with code"`, ...). CAQRS gets this
  for free — tool returns are typed pydantic models.
- **Loop response philosophy**: Mercury negotiates with the LLM by
  injecting `[SYSTEM WARNING]` messages in-context. CAQRS imposes
  hard limits at the orchestrator boundary; agents never see warning
  messages.
- **Live broker as default**: rejected. Execution is gated by
  `StrategyDecision` artifact emission only; broker adapters arrive
  in P3+ behind explicit human-approval workflow.

## Deliberately not imported (Mercury)

- **Soul / persona / taste / heartbeat 4-file model**
  (`src/soul/identity.ts` ADR-006). CAQRS agents have **roles**, not
  **personas**. Per-agent system prompts are ~150 tokens each
  (vs Mercury's ~500-token always-on baseline).
- **Channel multiplexing** (`src/channels/`, including Telegram
  integration). Research orchestrator runs in a single mode.
- **Daemonization + watchdog** (`src/cli/`). Research prototype runs
  in foreground.
- **Ink TUI** (ADR-002). CLI suffices for research operator.
- **Vercel AI SDK as the transport seam** (ADR-005). CAQRS uses
  `httpx` directly to control subscription credential paths
  (Anthropic / Codex CLI reuse) that the SDK does not surface.
- **Schedule tasks as AI-callable tools** (ADR-008). CAQRS cycles are
  launched explicitly by the operator, not autonomously by an agent.

## Imported from OpenClaw (MIT, by Peter Steinberger / contributors)

OpenClaw was surveyed at commit
`22c9e82e835f4ef2cb3807f7fe6e148f4535a5ec` for the subscription-
credential reuse paths.

| OpenClaw path | CAQRS port location | Status |
| --- | --- | --- |
| `extensions/anthropic/cli-auth-seam.ts` | `src/caqrs/providers/anthropic_cli.py` (P1.1.c.1) | merged |
| `extensions/anthropic/stream-wrappers.ts` (OAuth beta header set) | `src/caqrs/providers/anthropic_cli.py` (P1.1.c.1) | merged |
| `src/agents/anthropic-transport-stream.ts` (OAuth-vs-API-key marker) | `src/caqrs/providers/anthropic_cli.py` (P1.1.c.1) | merged |
| `extensions/openai/openai-codex-provider.ts` (route definition) | `src/caqrs/providers/codex_cli.py` (P1.1.c.2) | merged |
| `extensions/openai/base-url.ts` (`chatgpt.com/backend-api/codex` base URL) | `src/caqrs/providers/codex_cli.py` (P1.1.c.2) | merged |
| `src/infra/provider-usage.fetch.codex.ts` (auth/header pattern) | `src/caqrs/providers/codex_cli.py` (P1.1.c.2) | merged |
| `extensions/codex/src/app-server/auth-bridge.ts` (account_id usage) | `src/caqrs/providers/codex_cli.py` (P1.1.c.2) | merged |
| `src/agents/cli-credentials.ts` (Claude + Codex parsers, JWT exp decoder) | `src/caqrs/providers/_cli_creds.py` (P1.1.a + P1.1.b) | merged |

OpenClaw is MIT-licensed. Ported files carry
`SPDX-License-Identifier: MIT` headers and reference the originating
OpenClaw commit hash. See `docs/decisions/0002-provider-strategy.md`
for the compliance posture and `docs/decisions/0003-oauth-refresh-deferred.md`
for the auto-refresh trade-off.

## Code-sharing audit

No source files have been copied verbatim from Mercury or OpenClaw
into CAQRS. Where ideas are shared, the implementation is **rewritten
from scratch in idiomatic Python**, with citations in the relevant
file's docstring or in `docs/research/mercury-survey/`.

The OpenClaw entries above are described in this document as
"merged" because they are already implemented in the CAQRS provider
layer. The Mercury entries are "planned" because they inform P1.2
through P3 phases that have not yet been implemented.

If at any point a CAQRS file is constructed by line-by-line port (vs
ground-up rewrite), it must:

1. Carry an `SPDX-License-Identifier: MIT` header at the top.
2. Cite the upstream file path and commit hash in a docstring.
3. Be added as a row in this document under the appropriate
   "Imported" table.
4. Discharge the upstream license obligations (MIT requires
   reproduction of the copyright notice and license, which CAQRS does
   via the project root `NOTICE` file).
