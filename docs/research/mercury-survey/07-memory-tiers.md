# 07 — Memory Tiers (Base) (`src/memory/store.ts`)

## Mercury source

- File: `src/memory/store.ts`
- Lines: 206 (complete read).
- Note: this is the **base / flat-file** memory layer. The richer
  second-brain layer with autonomous merge / SQLite / FTS5 lives in
  separate files and is covered in file 08.

## Key types

```ts
interface MemoryEntry { id; timestamp; role: 'user'|'assistant'|'system'; content; tokenCount?; reasoning?; metadata?; }
interface LongTermFact { id; timestamp; topic; fact; source; }
interface EpisodicEvent { id; timestamp; type; summary; channelType; metadata?; }

class ShortTermMemory  { add(conv, entry); getRecent(conv, n); clear(conv); }
class LongTermMemory   { add(fact); search(query, limit); getAll(); }
class EpisodicMemory   { record(event); getRecent(n); prune(daysCutoff); }

function migrateLegacyMemory(): void;
```

Storage:

- Short-term: per-conversation JSON file at
  `~/.mercury/memory/short-term/{conversationId}.json`. Capped at
  `config.memory.shortTermMaxMessages` (default 10).
- Long-term: single JSONL file at
  `~/.mercury/memory/long-term/facts.jsonl`. Append-only.
- Episodic: single JSONL file at
  `~/.mercury/memory/episodic/events.jsonl`. Append-only with
  prune-on-rewrite.

## Implementation patterns

### 1. Per-conversation short-term cap

```ts
class ShortTermMemory {
  add(conversationId, entry) {
    const messages = this.conversations.get(conversationId) ?? this.loadFromDisk(conversationId);
    messages.push(entry);
    if (messages.length > this.maxMessages) {
      messages.splice(0, messages.length - this.maxMessages);
    }
    this.saveToDisk(conversationId, messages);
  }
}
```

Each conversation has its own file. The cap is enforced in-memory
**and** persisted on every write (atomic from agent's perspective —
no separate flush step).

CAQRS analogue: short-term memory at the **cycle level** (not
per-conversation). Each research cycle's recent artifacts (last N
hypothesis cards, last N backtest reports) are accessible to
downstream agents for context. Capacity ~10-20 artifacts per cycle.

### 2. Long-term keyword search (lines 129-138)

```ts
search(query, limit = 5): LongTermFact[] {
  const lowerQuery = query.toLowerCase();
  const terms = lowerQuery.split(/\s+/);
  return this.facts
    .filter(f => {
      const text = `${f.topic} ${f.fact}`.toLowerCase();
      return terms.some(t => text.includes(t));
    })
    .slice(-limit);
}
```

Naive substring OR-match across topic + fact. Returns the **last** N
matches (most recent). Acceptable when the long-term store has
~hundreds of facts; fails at scale (which is why ADR-010 introduced
SQLite + FTS5 for the second brain).

CAQRS analogue: simple long-term store for **immutable research
constants** (e.g., `_known_macro_regime_thresholds.json`,
`_data_source_reliability_scores.json`). Use SQLite + FTS5 for the
larger learnable memory (file 08).

### 3. Episodic prune-by-age (lines 181-190)

```ts
prune(olderThanDays = 7): number {
  const cutoff = Date.now() - olderThanDays * 24 * 60 * 60 * 1000;
  const before = this.events.length;
  this.events = this.events.filter(e =>
    e.timestamp >= cutoff || e.metadata?.important
  );
  const removed = before - this.events.length;
  if (removed > 0) {
    writeFileSync(this.filepath, this.events.map(e => JSON.stringify(e)).join('\n') + '\n', 'utf-8');
  }
  return removed;
}
```

Two important details:

- `metadata.important` flag exempts events from pruning. The agent
  can mark an episode as important when recording.
- Rewrite-on-prune: the JSONL is fully rewritten when any pruning
  happens. For ~1k events/day this is fine; at scale you would want
  rotating files.

CAQRS analogue: episodic = **one entry per cycle** (cycle started,
hypothesis emitted, backtest ran, decision made, ...). Important
episodes (e.g., "first profitable cycle", "first regime change
detected") get the flag. Prune at 30-90 days for non-important.

### 4. JSON vs JSONL choice

- Short-term: full JSON array per conversation (small, capped).
- Long-term + Episodic: JSONL (append-only, log-friendly).

Rationale: short-term needs random access (last N), long-term and
episodic need append-only writes (no read-modify-write race).

CAQRS analogue: same split. Cycle-state JSON, append-only artifact
log JSONL.

### 5. Lazy loading + in-memory caching

`ShortTermMemory.conversations: Map<string, MemoryEntry[]>` is lazily
populated as conversations come in. Once loaded, the disk is the
authoritative store but reads come from memory.

`LongTermMemory` and `EpisodicMemory` load their full files on
construction. This is fine for ≤10k entries; at larger scale the
patterns would need to be index-backed.

CAQRS analogue: per-cycle state can stay in memory; long-term and
episodic stores should switch to SQLite immediately (do not pay the
"start with JSONL, migrate later" cost — second brain already
demonstrates the migration pain).

### 6. Legacy migration (lines 7-27)

```ts
export function migrateLegacyMemory(): void {
  const legacyDir = resolve('memory');     // project-local
  const newDir = getMemoryDir();            // ~/.mercury/memory/
  if (!existsSync(legacyDir) || legacyDir === newDir) return;
  if (!existsSync(join(legacyDir, 'short-term')) /* etc */) return;
  // copy then remove
}
```

Mercury moved from project-local `./memory/` to user-local
`~/.mercury/memory/` and ships a one-time migration. CAQRS does not
have this baggage; place memory in `~/.caqrs/memory/` from day one.

## What CAQRS already has

Nothing yet. The current `RunMetadata` (P0) records lineage but no
persistent storage layer is wired up.

## CAQRS implications

```
caqrs/memory/
├── short_term.py        # per-cycle recent artifacts (in-memory + JSON snapshot)
├── episodic.py          # JSONL append-only event log + prune
├── long_term.py         # SQLite with FTS5 from day one
├── second_brain.py      # autonomous learnable memory (file 08 covers)
└── retrieve.py          # cross-tier retrieval API used at prompt-build time
```

Per-tier responsibility split:

| Tier | Stores | Lifetime | Backing | CAQRS use |
| --- | --- | --- | --- | --- |
| Short-term | recent artifacts in current cycle | cycle | in-memory + JSON | recent context for next agent in pipeline |
| Episodic | per-cycle events | 30-90 days | JSONL append + prune | audit log, regret analysis |
| Long-term | named facts (regime thresholds, data quality) | indefinite | SQLite | strategy parameters that have stabilised |
| Second brain | learnable user/strategy model | autonomous | SQLite + FTS5 | failed hypotheses, accepted strategies, lessons |

## Open questions

- Mercury's short-term is per-conversation; CAQRS's is per-cycle.
  But **a research cycle can span hours** (Observer ➔ Hypothesis ➔
  Skeptic ➔ Research ➔ Auditor ➔ Decision), so the in-memory
  representation must persist across orchestrator restarts within a
  cycle. Snapshot to JSON on every artifact emission (cheap).
- Mercury caps short-term at 10 messages. CAQRS may need larger caps
  per cycle (the agent pipeline can produce 5-10 artifacts in a
  single cycle, plus their parents). Recommend cap at 50 artifacts
  per cycle, prune older first.
- Episodic prune cutoff: Mercury defaults to 7 days. For research
  audit, **30 days** seems more appropriate (the regret analysis
  window). Important episodes (live trades, regime changes, large
  PnL events) exempt from prune.
