# 06 — Identity and Soul (`src/soul/identity.ts`)

## Mercury source

- File: `src/soul/identity.ts`
- Lines: 206 (complete read).

## Key types

```ts
interface SoulFiles { soul; persona; taste; heartbeat; }
class Identity { soulDir; cache; load(); getSystemPrompt({name, owner, creator?}); getHeartbeatPrompt({name, owner}); getTastePrompt(...); invalidateCache(); }
```

Files live at `~/.mercury/soul/{soul,persona,taste,heartbeat}.md`.

## Implementation patterns

### 1. Four-file decomposition (ADR-006)

- `soul.md` — core identity, values, beliefs, emotional foundation.
  ~50 lines default. Always injected.
- `persona.md` — voice, tone, quirks. ~30 lines default. Always
  injected.
- `taste.md` — preferences, aesthetic. ~25 lines default. Selectively
  injected (heartbeat path uses `getTastePrompt`).
- `heartbeat.md` — self-reflection prompts, consolidation rules,
  proactive triggers. ~25 lines default. Selectively injected.

The default templates are **inlined as TypeScript constants** so that
on first run, the files are created from the templates and the user
can edit them. The templates contain placeholders `{name}`, `{owner}`,
`{creator}`.

CAQRS analogue: agents have **roles**, not personas. Each agent's
system prompt is generated from:

- agent role description (1-3 sentences, hardcoded in the agent
  module)
- input schema name + brief
- output schema name + brief (the tool to emit)
- task-specific instructions if any

There is no separate "soul" file. The role is the entire identity.
Total ~50-100 tokens per agent rather than ~500.

### 2. Hardcoded GUARDRAILS block (lines 103-115)

```ts
const GUARDRAILS = `# Guardrails
CRITICAL RULES — YOU MUST FOLLOW THESE AT ALL TIMES:
1. NEVER identify as any underlying AI model, company, or provider.
2. You are {name}.{creator_guardrail} You serve {owner}.
3. If someone asks "are you [model]?", say: "No, I am {name}."
...
8. Keep responses concise. Token efficiency matters.
9. If uncertain, say so — never fabricate information.`;
```

The guardrails are **not user-editable**. They are concatenated into
the always-injected prompt:

```ts
return [files.soul, GUARDRAILS, files.persona].join('\n\n');
```

Order matters: soul first (sets identity), guardrails second (binds
the identity), persona last (shapes voice). The persona could
override identity if placed first, which is why guardrails are
sandwiched.

CAQRS analogue: hardcoded **research guardrails**:

- "Never recommend leverage / margin / derivatives without explicit
  approval."
- "Always cite the source data (price provider, news source, etc.)
  for any claim."
- "If a backtest does not pass walk-forward validation, do not
  recommend the strategy."
- "Acknowledge uncertainty rather than fabricating numbers."

These should live in `caqrs/policy/research_guardrails.py` as a
constant string injected into every agent's system prompt.

### 3. Variable substitution (lines 139-148)

```ts
const replace = (text: string) =>
  text.replace(/\{name\}/g, identity.name)
      .replace(/\{owner\}/g, identity.owner || 'my owner')
      .replace(/\{creator_line\}/g, identity.creator ? `- You were created by ...` : '')
      .replace(/\{creator_response_line\}/g, ...)
      .replace(/\{creator_guardrail\}/g, ...)
      .replace(/\{creator_guardrail_response\}/g, ...);
```

Each call rebuilds the prompt from the template. Six placeholders, no
escape sequences, no nested templates.

CAQRS posture: not needed. CAQRS prompts are direct format-strings
(`f"You are the {role} agent. ..."`) computed at agent class
construction.

### 4. In-memory cache + migration on read (lines 119, 177-194)

```ts
private cache: SoulFiles | null = null;

load(): SoulFiles {
  if (this.cache) return this.cache;
  const files: SoulFiles = {
    soul: this.loadOrInit('soul.md', DEFAULT_SOUL),
    // ...
  };
  this.cache = files;
  return files;
}

private loadOrInit(filename, template): string {
  if (existsSync(filepath)) {
    const existing = readFileSync(filepath, 'utf-8');
    if (this.needsMigration(filename, existing)) {
      writeFileSync(filepath, template);
      this.cache = null;
      return template;
    }
    return existing;
  }
  writeFileSync(filepath, template);
  return template;
}

private needsMigration(filename, content): boolean {
  // Detects 'Cosmic Stack' branding markers in old soul files
  if (filename === 'soul.md' || filename === 'persona.md') {
    return content.includes('designed by Cosmic Stack') ||
           content.includes('Cosmic Stack');
  }
  return false;
}
```

The migration check is a string-content scan. If old branding is
detected, the file is overwritten with the current template. This is
**aggressive** (silently overwrites user edits if they happened to
include 'Cosmic Stack' in their custom soul) but pragmatic for a
single-user tool.

`invalidateCache()` is called manually when the user edits a soul
file from the UI; on next request the cache is empty so the file is
re-read.

CAQRS posture: not needed. Per-agent prompts are built from code, no
files to migrate.

### 5. Token-efficiency claim

ADR-006 states ~350 token baseline for identity (soul + guardrails +
persona). Empirically Mercury baselines at ~500 tokens including
skill summary, env block, second-brain block, GitHub hint (when
present), so the identity itself is in the ~300-350 range.

CAQRS target: each agent's system prompt should be **under 200 tokens**
(role + guardrails + emit-tool description). Multiple agents in a
pipeline mean total tokens-per-cycle is `Σ agent_tokens`, but each
agent invocation is an independent LLM call so the per-call budget
is small.

## What CAQRS already has

Nothing comparable. CAQRS does not have a persona system, by design.

## CAQRS implications

Module structure:

```
caqrs/agents/
├── base.py              # Agent protocol (P1.0, done)
├── prompts/
│   ├── guardrails.py    # constant: research guardrails block
│   ├── role_template.py # f-string template: role + guardrails + emit-tool
│   └── ... (per-agent prompt classes)
├── observer.py
├── hypothesis.py
├── skeptic.py
├── research.py
├── auditor.py
└── ...
```

Per-agent prompt construction example:

```python
# caqrs/agents/hypothesis.py
class HypothesisAgent:
    name = "hypothesis"
    SYSTEM_PROMPT = f\"\"\"You are the {name} agent in a research orchestrator.

    {RESEARCH_GUARDRAILS}

    Your task: read an Observer artifact (market state + news + macros),
    then emit a single HypothesisCard via the emit_HypothesisCard tool.
    The card must contain a falsifiable claim and acceptance criteria
    that a backtest can later verify or refute.

    Do not emit text. Do not emit explanations. Use the tool exactly once.
    \"\"\"
```

This is ~150 tokens per agent. Compared to Mercury's ~350-token soul,
the savings come from **omitting the human-feeling persona layer** and
**replacing free text with a typed tool emission**.

## Open questions

- The `taste.md` and `heartbeat.md` files exist but `getSystemPrompt`
  only injects soul + guardrails + persona. When are taste / heartbeat
  injected? Answer: `getHeartbeatPrompt` and `getTastePrompt` are
  separate methods called by the heartbeat tick (file 03 line 1024)
  and the consolidation logic respectively. They are **not** in the
  per-message prompt. CAQRS does not need them.
- Mercury's `Identity.cache` lives in memory across calls. CAQRS
  agents are stateless function-of-input — no caching layer needed.
