# 09 — Skill Progressive Disclosure (`src/skills/loader.ts`)

## Mercury source

- File: `src/skills/loader.ts`
- Lines: 156 (complete read).

## Key types

```ts
interface SkillMeta { name; description; version; allowed-tools?: string[]; }
interface SkillDiscovery { name; description; }   // light-weight summary
interface Skill { ...SkillMeta; instructions; scriptsDir?; referencesDir?; }

class SkillLoader {
  skillsDir: string;
  discovered: Map<string, SkillDiscovery>;  // names + descriptions
  loaded: Map<string, Skill>;                // full bodies
}
```

Skill directory layout (`~/.mercury/skills/<skill-name>/`):

```
<skill-name>/
├── SKILL.md             # required: YAML frontmatter + markdown body
├── scripts/             # optional: executable scripts
└── references/          # optional: reference documents
```

## Implementation patterns

### 1. Two-phase loading: discover() + load()

```ts
discover(): SkillDiscovery[] {
  // 1. Walk skillsDir at startup
  // 2. For each entry, read SKILL.md
  // 3. Parse YAML frontmatter
  // 4. Store {name, description} only — not the full body
}

load(name: string): Skill | null {
  if (cached) return cached;
  // Walk again, find matching name, parse FULL content
  // Cache loaded skill
}
```

The agent's system prompt only includes the `SkillDiscovery`
summaries (`Available skills:\n- foo: does X\n- bar: does Y`). Full
skill instructions are loaded only when the LLM calls `use_skill(foo)`.

This is **the canonical progressive-disclosure pattern**:

- ~5 tokens per skill at startup (name + description bullet)
- ~hundreds-of-tokens per skill on activation (full instructions)

### 2. Frontmatter parser (lines 10-26)

```ts
function parseSkillMd(content: string): { meta; instructions } | null {
  const fmMatch = content.match(/^---\s*\n([\s\S]*?)\n---\s*\n([\s\S]*)$/);
  if (!fmMatch) return null;
  const meta = parseYaml(fmMatch[1]);
  const instructions = fmMatch[2].trim();
  if (!meta.name || !meta.description) return null;
  return { meta, instructions };
}
```

Strict regex: starts with `---\n`, captures YAML to next `---\n`, the
rest is markdown body. **Both `name` and `description` are required**;
missing either invalidates the skill.

### 3. Underscore prefix as "skip" (lines 47-48, 73)

```ts
if (!entry.isDirectory() || entry.name.startsWith('_')) continue;
```

Directories starting with `_` are skipped during discovery and
loading. This is how `_template/` (the seeded example skill) is
hidden from the agent. Pure convention; no separate manifest field.

### 4. Auto-seeded template on first run (lines 120-156)

If `~/.mercury/skills/` does not exist, the loader creates it and
writes a `_template/SKILL.md` example. The user can copy this dir to
create their own skill. The template references `allowed-tools` to
explain the elevation system.

### 5. `saveSkill(name, content)` for runtime install (lines 109-118)

```ts
saveSkill(name: string, content: string): string {
  const skillDir = join(this.skillsDir, name);
  mkdirSync(skillDir, { recursive: true });
  writeFileSync(join(skillDir, 'SKILL.md'), content, 'utf-8');
  this.discover();   // re-scan to pick up the new skill
  return skillDir;
}
```

Used by the `install_skill` tool: paste markdown content into a
prompt, the agent calls `install_skill`, the loader writes the file
and re-scans. **Re-discovery is forced** so the new skill is
immediately usable.

### 6. Lazy in-memory cache

`loaded: Map<string, Skill>` is populated on first `load()` call, kept
forever. Mercury does not invalidate this cache when the user edits
SKILL.md files manually — a process restart is required to pick up
edits. (This is documented; not surveyed in test files.)

## What CAQRS already has

Nothing. Skills are out of scope for P1.

## CAQRS implications

The progressive-disclosure pattern transfers cleanly to **research
playbooks**:

```
~/.caqrs/playbooks/
├── 12-1-momentum-us-large-cap/
│   ├── PLAYBOOK.md          # frontmatter (name, description, universe, horizon, version)
│   ├── strategy.py          # signal computation
│   └── references/
│       └── jegadeesh-titman-1993.md
├── earnings-drift-small-cap/
│   └── PLAYBOOK.md
└── _template/
    └── PLAYBOOK.md
```

The Hypothesis Agent's system prompt would include only:

```
Available research playbooks:
- 12-1-momentum-us-large-cap: 12-month return minus most recent month, on US large caps
- earnings-drift-small-cap: post-earnings-announcement drift on small caps
```

When the agent decides to investigate one, it calls `use_playbook(name)`
(analogous to `use_skill`), the full PLAYBOOK.md body is injected into
context, and the agent proceeds.

**However: out of scope for P1.2**. CAQRS agents in P1.2 are
domain-specific roles (Observer, Hypothesis, Skeptic, Research,
Auditor), not playbook executors. Playbooks emerge later when the
Research Agent needs to instantiate concrete strategies.

## Open questions

- Mercury's skills can declare `allowed-tools` which permission
  manager uses for elevation (file 04). CAQRS playbooks could
  declare `required-data-sources` and `risk-envelope` similarly,
  letting the Policy Gateway elevate per-playbook.
- The 5-line description-only summary scales well to ~50 skills.
  Beyond that, **categorisation** would help (e.g., `momentum`,
  `mean-reversion`, `event-driven`). Defer.
- **CAQRS playbooks need parameters** in a way Mercury's skills do
  not (e.g., lookback window, position sizing rule). The PLAYBOOK.md
  frontmatter would need a `parameters` schema (pydantic). Tackle in
  P2 or later.
