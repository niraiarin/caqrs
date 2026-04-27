# ADR-0002: Subscription-credential reuse via the OpenClaw pattern

- **Status**: Accepted
- **Date**: 2026-04-27

## Context

CAQRS needs an LLM provider layer that:

1. Uses the user's existing **Anthropic Claude subscription** without
   pay-as-you-go API billing.
2. Uses the user's existing **OpenAI ChatGPT (Codex) subscription** under
   the same constraint.
3. Routes to a **local LiteLLM gateway** for self-hosted models.

After Anthropic's 2026-04-04 policy enforcement against subscription-credential
extraction by third-party harnesses, naive OAuth-token reuse is no longer
ToS-compliant. OpenClaw's documentation states that
*"OpenClaw-style Claude CLI usage is allowed again"* per Anthropic staff
communication, and OpenClaw exposes the corresponding code paths under
`extensions/anthropic/cli-auth-seam.ts` and
`extensions/codex/src/app-server/auth-bridge.ts` (both MIT-licensed).

## Decision

CAQRS ports OpenClaw's CLI-credential-reuse pattern into Python under
`src/caqrs/providers/`:

- `AnthropicViaClaudeCLI` — port of OpenClaw `extensions/anthropic/`.
- `OpenAIViaCodexCLI` — port of OpenClaw `extensions/codex/src/app-server/`.
- `OpenAICompatProvider` — generic OpenAI-compatible HTTP for the user's
  LiteLLM gateway and similar.
- `ProviderRegistry` — ordered fallback chain, warm-path bias on
  last-successful provider (matches Mercury's `src/providers/registry.ts`).

Ported files will carry an `SPDX-License-Identifier: MIT` header and a
reference to the originating OpenClaw commit hash. The lineage is recorded
in `docs/lineage.md` alongside the Mercury references.

## Phasing

- **P1.0** (this commit): Protocol, registry, three stub providers,
  protocol-shape + registry-fallback tests. No real network or filesystem
  access.
- **P1.1**: Port OpenClaw `extensions/anthropic/` and `extensions/codex/`
  to populate `AnthropicViaClaudeCLI` and `OpenAIViaCodexCLI`.
- **P1.1.5**: Implement `OpenAICompatProvider` over httpx async + tool-call
  structured output.
- **P1.2**: Wire the registry into the Hypothesis / Skeptic / Auditor agents.

## Alternatives rejected

- **Run OpenClaw as an out-of-process gateway**: rejected. OpenClaw is a
  coding agent, not a request router; using it as a generic LLM proxy is
  outside its design intent and would couple CAQRS to OpenClaw's CLI
  evolution rather than just its auth code.
- **Route all traffic through LiteLLM**: rejected for now. LiteLLM expects
  upstream API keys and does not natively reuse `claude` / `codex` CLI
  credentials. LiteLLM remains in scope for the local-model `OpenAICompatProvider`.
- **ClawRouter or `openclaw-hub` as a router**: rejected for the same
  reason; auth handling there targets blockchain/USDC payments, not
  subscription-credential reuse.

## Compliance posture

This ADR records that CAQRS adopts the same compliance posture as OpenClaw:
subscription credentials are accessed only via the official CLI's stored
session (`~/.claude/`, `~/.codex/sessions`), never via OAuth-token
extraction or browser-cookie scraping. If Anthropic or OpenAI policy
changes, the corresponding provider must be disabled at the registry
level until the port is updated.

`LICENSE_AND_TOS.md` is the canonical place to record any future
policy/ToS revisions affecting these providers.

## Consequences

### Positive

- All three subscription/local sources are usable from one typed interface.
- Per-call cost / latency / token counts feed `RunMetadata` directly.
- Fallback is structural rather than judgmental.

### Negative

- Maintenance burden tracks OpenClaw's `extensions/` evolution. Mitigated
  by pinning the port to a specific commit hash and documenting the diff
  when re-syncing.

### Risks

- `claude` / `codex` CLI credential layouts may change without notice.
  Detected by the smoke tests planned for P1.1; on detection, the affected
  provider falls through to the next via the existing `ProviderError` flow.
