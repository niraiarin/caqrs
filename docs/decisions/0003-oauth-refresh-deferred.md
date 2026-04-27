# ADR-0003: OAuth refresh deferred to manual re-login

- **Status**: Accepted
- **Date**: 2026-04-27
- **Supersedes**: clause in ADR-0002 anticipating auto-refresh in P1.1.d

## Context

P1.1.d originally scoped automatic OAuth token refresh for the
subscription-backed providers (`AnthropicViaClaudeCLI`,
`OpenAIViaCodexCLI`). The natural flow when a stored credential
expires:

1. Detect expiry (or HTTP 401)
2. Exchange the stored `refresh_token` at the upstream OAuth token endpoint
3. Persist the new `access_token` (file or Keychain)
4. Retry the failed request

The blocker: **neither Anthropic's nor OpenAI/ChatGPT's OAuth token
endpoint is publicly documented**. OpenClaw — the reference
implementation we are porting from — delegates the entire refresh
mechanism to a **closed-source** npm package (`@mariozechner/pi-ai`):

```typescript
// src/agents/auth-profiles/oauth-manager.ts
export type OAuthManagerAdapter = {
  refreshCredential: (credential: OAuthCredential)
    => Promise<OAuthCredentials | null>;
  // ↑ implementation lives in pi-ai
};
```

Reverse-engineering the endpoints (from pi-ai bytecode, or by sniffing
CLI network traffic) carries operational risk: endpoints can change
without notice, auth flows may include undocumented anti-abuse signals,
and a wrong-guessed POST may invalidate the stored `refresh_token`.

## Decision

P1.1.d implements **pre-flight expiry detection + manual re-login
guidance** instead of auto-refresh:

- On every `complete()` call, the credential's `expires_at_ms` is
  compared to wall-clock time with a 60-second skew margin (so tokens
  that would expire mid-request are treated as already expired).
- An expired credential raises `AuthError` carrying the exact expiry
  timestamp and the renewal command (`claude login` / `codex login`).
- The orchestrator's `ProviderRegistry` falls through to the next
  provider on `AuthError` like any other auth failure.

## Consequences

### Positive

- Zero risk to upstream OAuth flows. The user's `refresh_token` is
  never consumed except by the official CLI.
- Clearer failure signal than HTTP 401: the error names the expiry
  timestamp and the renewal command. No silent retry storms.

### Negative

- Long-running CAQRS sessions require a human-driven re-login when
  tokens expire (typically every 1h–24h depending on provider).
- The orchestrator cannot self-heal without the next provider in the
  registry being able to handle the request.

### Mitigation

- CAQRS is currently research-prototype scoped: one user, one machine,
  agent loops complete in minutes. The expiry rate is low.
- The error message names the precise renewal command so re-login
  is one shell line away.
- `is_configured()` continues to work for "do I have any credentials at
  all" checks and is stable across expiries.

## Reconsider when

- `@mariozechner/pi-ai` becomes open-source (or a Python equivalent
  emerges), **or**
- Anthropic / OpenAI publish their OAuth token endpoints, **or**
- CAQRS evolves into a long-running daemon and manual re-login becomes
  a significant operational pain point.

## Implementation locus

- `src/caqrs/providers/_cli_creds.py`: `is_cred_fresh()` and
  `format_expiry_iso()` helpers.
- `src/caqrs/providers/anthropic_cli.py`,
  `src/caqrs/providers/codex_cli.py`: pre-flight check inside
  `complete()` raising `AuthError` with the renewal command.
