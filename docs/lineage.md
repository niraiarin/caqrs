# Design lineage: from Mercury to CAQRS

CAQRS is **not** a fork of Mercury. However, several design principles are
imported. This document records what was borrowed and what was deliberately
changed.

## Imported

| Mercury idea                                                  | CAQRS adaptation                                                              |
| ------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| Permission gateway (`src/capabilities/permissions.ts`)        | Policy Gateway (P3): asset / position / loss-limit projections on `Decision` |
| Agentic loop with explicit state machine (`src/core/agent.ts`)| Orchestrator state machine; agents are typed pure functions, no hidden state  |
| Tool-call loop detector (25-call hard cap)                    | Per-agent budget enforced by orchestrator; same blast-radius philosophy       |
| Second Brain (SQLite + FTS5)                                  | Episodic memory store for failed hypotheses + regime fingerprints             |
| Artifact-based design (`~/.mercury/` layout)                  | All artifacts pydantic-typed, versioned (`schema_version`), parent-linked     |
| Graceful provider fallback (`src/providers/registry.ts`)      | Same: ordered provider chain, last-successful caching                          |

## Deliberately changed

- **Language**: TypeScript → Python. The financial-research stack (vectorbt,
  pandas, statsmodels, FRED / yfinance / PRAW SDKs) cannot be cross-compiled
  from TypeScript at any reasonable cost.
- **Channel-agnostic UI** (Mercury's CLI ⇄ Telegram parity): dropped. CAQRS
  is a research workstation, not a conversational assistant.
- **Live broker as default**: rejected. Execution is gated by `StrategyDecision`
  artifact emission only; broker adapters arrive in P3+ behind explicit
  human-approval workflow.
- **Soul / persona / taste / heartbeat split**: dropped. CAQRS has no
  conversational persona; the agent persona is collapsed into a research
  policy document inside `RunMetadata.agent_name`.

## Not imported

- Telegram channel, daemon mode, watchdog. These are Mercury-specific UX
  concerns with no analogue in a research orchestrator.
- Skill loader (Mercury's progressive-disclosure skill body fetch). CAQRS
  agents are statically registered; dynamic skill loading is out of scope.
- Token-budget global heartbeat. CAQRS uses per-agent budgets enforced at
  orchestrator boundaries instead.

## Imported (OpenClaw, MIT)

| OpenClaw path                                                | CAQRS port (planned)                                  |
| ------------------------------------------------------------ | ----------------------------------------------------- |
| `extensions/anthropic/cli-auth-seam.ts`                      | `src/caqrs/providers/anthropic_cli.py` (P1.1)         |
| `extensions/anthropic/cli-backend.ts`                        | `src/caqrs/providers/anthropic_cli.py` (P1.1)         |
| `extensions/codex/src/app-server/auth-bridge.ts`             | `src/caqrs/providers/codex_cli.py` (P1.1)             |
| Provider registry pattern (`api: "openai-completions" \| "anthropic-messages"`) | `src/caqrs/providers/base.py` + `registry.py` |

OpenClaw is MIT-licensed. Ported files carry `SPDX-License-Identifier: MIT`
and a reference to the originating OpenClaw commit hash. See
`docs/decisions/0002-provider-strategy.md` for the compliance posture.

## Code-sharing audit

No source files have been copied from Mercury into CAQRS. Where ideas are
shared, the implementation is rewritten from scratch in idiomatic Python.

OpenClaw code paths cited above will be ported (not vendored) in P1.1.
The port adds an SPDX header and a commit-hash anchor in each file's
docstring; the audit table in this document is the authoritative log of
what was ported and when.

If at any point code is copied verbatim from any source, it must be
marked with the upstream file path and commit hash in a comment, the
relevant section recorded here, and the upstream license obligations
discharged in the file header.
