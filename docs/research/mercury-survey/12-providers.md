# 12 — Providers (`src/providers/base.ts` + `registry.ts`)

## Mercury source

- File: `src/providers/base.ts`
- Lines: 33 (complete read).
- File: `src/providers/registry.ts`
- Lines: 91 (complete read).

## Key types

```ts
// base.ts
interface LLMResponse  { text; inputTokens; outputTokens; totalTokens; model; provider; }
interface LLMStreamChunk { text; done; }

abstract class BaseProvider {
  abstract readonly name: string;
  abstract readonly model: string;
  protected config: ProviderConfig;
  abstract generateText(prompt, systemPrompt): Promise<LLMResponse>;
  abstract streamText(prompt, systemPrompt): AsyncIterable<LLMStreamChunk>;
  abstract isAvailable(): boolean;
  abstract getModelInstance(): any;
}

// registry.ts
class ProviderRegistry {
  providers: Map<string, BaseProvider>;
  defaultName: string;
  lastSuccessful: string | null;
  get(name?): BaseProvider | undefined;
  getDefault(): BaseProvider;        // last-success-biased
  getFallbackIterator(): IterableIterator<BaseProvider>;
  markSuccess(name): void;
  listAvailable(): string[];
  hasProviders(): boolean;
}
```

## Implementation patterns

### 1. Abstract class with 4 abstract methods

`BaseProvider` is a class hierarchy, not a structural protocol.
Concrete subclasses: `OpenAICompatProvider`, `AnthropicProvider`,
`DeepSeekProvider`, `OllamaProvider`. Each:

- Sets `name` and `model` from config.
- Implements `generateText` / `streamText` via Vercel AI SDK adapters.
- Implements `isAvailable()` — checks config presence (API key, base
  URL).
- Implements `getModelInstance()` — returns the AI SDK's model
  object that `generateText`/`streamText` consume.

CAQRS analogue: P1.0 `LLMProvider` is a `Protocol[I, O]` (PEP 695),
not an abstract class. Structural typing is more flexible — adapter
classes don't need to inherit. Otherwise the shape is similar.

### 2. Registry construction from config (lines 15-46)

```ts
const entries: ProviderConfig[] = [
  config.providers.deepseek,
  config.providers.openai,
  config.providers.anthropic,
  config.providers.grok,
  config.providers.ollamaCloud,
  config.providers.ollamaLocal,
];

for (const pc of entries) {
  if (!isProviderConfigured(pc)) continue;
  // class dispatch by pc.name
  this.providers.set(pc.name, provider);
}
```

The registry is **constructor-built**: walks a fixed list of provider
slots, instantiates each that is configured, and stores in a Map.

This is **less flexible** than CAQRS's approach (P1.0 takes a tuple of
already-instantiated providers, not config). CAQRS's approach
separates "construct providers" from "register them in fallback
order", which is cleaner for testing.

### 3. `getDefault()` with last-success bias (lines 53-66)

```ts
getDefault(): BaseProvider {
  if (this.lastSuccessful) {
    const provider = this.providers.get(this.lastSuccessful);
    if (provider) return provider;
  }
  const provider = this.providers.get(this.defaultName);
  if (!provider) {
    const first = this.providers.values().next().value;
    if (!first) throw new Error('No LLM providers...');
    return first;
  }
  return provider;
}
```

Three-tier lookup:

1. Last-successful provider, if known.
2. Configured default provider.
3. First registered provider (fallback).

CAQRS's `ProviderRegistry.complete()` uses an "ordered indices"
helper that puts the warm path first and tries others in order.
**Same idea, slightly different surface**: Mercury exposes
`getDefault` and `getFallbackIterator` as separate methods; CAQRS
hides this inside a single `complete()` call.

### 4. `getFallbackIterator()` (lines 68-78)

```ts
getFallbackIterator(): IterableIterator<BaseProvider> {
  const ordered: BaseProvider[] = [];
  const defaultProvider = this.getDefault();
  ordered.push(defaultProvider);
  for (const [, provider] of this.providers) {
    if (provider !== defaultProvider) ordered.push(provider);
  }
  return ordered[Symbol.iterator]();
}
```

The agent loop iterates this in `for...of`, breaking on first success
or hard-aborting on loop detection. **The iterator only includes
configured providers** — no auto-discovery, no probing.

CAQRS analogue: `_ordered_indices()` in `caqrs/providers/registry.py`
does the equivalent. Implementation differs but behaviour matches.

### 5. `markSuccess(name)` is the only state mutation

```ts
markSuccess(name: string): void { this.lastSuccessful = name; }
```

Called by the agent loop after a successful generation. **The
registry has no concept of "provider failed" or "provider rate-
limited"** — the agent decides which provider to skip in the next
attempt by trying the iterator. This is a deliberate simplicity
choice; the alternative (per-provider failure counters, exponential
back-off, breaker patterns) is heavier.

CAQRS analogue: same approach. Failures are surfaced via
`ProviderError` subclasses; the registry falls through and updates
last-successful only on success.

## What CAQRS already has

`caqrs/providers/registry.py` — already implements the
last-success-biased fallback chain. The implementation is **richer
than Mercury's**:

- PEP 695 generic `complete[T]` method instead of separate
  `generateText` / `streamText`.
- Typed `ProviderError` hierarchy (auth / rate / network / parse /
  schema_violation).
- Explicit `_ordered_indices()` helper for deterministic warm-path
  ordering.

Mercury's registry has features CAQRS does not yet have:
- `hasProviders()` boolean for "are any registered" checks.
- `listAvailable()` for introspection.
- `get(name?)` for explicit name lookup.

These are nice-to-haves; not strictly needed for P1 but would be
small additions.

## CAQRS implications

The CAQRS provider layer is **already more sophisticated** than
Mercury's. No code to port. The only outstanding items are minor
introspection methods (`available_provider_ids()`, `has_providers()`)
which can be added trivially when needed.

The bigger lesson is **what Mercury's BaseProvider does NOT do**:

- No retry logic (Vercel AI SDK provides this).
- No streaming back-pressure (delegated to AI SDK + channel adapter).
- No cost calculation (delegated to per-provider implementations or
  external `tokenBudget`).
- No caching (Anthropic prompt caching is set in
  `anthropic-payload-policy.ts`, not here).

The provider layer is **deliberately thin**. CAQRS should keep the
same posture: provider = transport + auth + structured-output
parsing. Cost rates, caching strategies, batch optimisation all live
elsewhere.

## Open questions

- Should CAQRS expose `available_provider_ids()` and
  `has_providers()`? **Yes, when first needed** by the orchestrator
  (P1.4). Trivial addition.
- Mercury throws if no providers are registered ("No LLM providers
  available"). CAQRS's registry constructor raises if the tuple is
  empty. Same end behaviour.
- Mercury's `getModelInstance()` returns `any` — the Vercel AI SDK's
  opaque model object. CAQRS does not have this concept (we hand-roll
  HTTP), so no analogue needed.
