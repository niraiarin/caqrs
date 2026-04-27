# 08 — Second Brain (`user-memory.ts` + `second-brain-db.ts`)

## Mercury source

- File: `src/memory/user-memory.ts` (697 lines, complete read)
- File: `src/memory/second-brain-db.ts` (379 lines, complete read)
- Companion ADR: ADR-010 (file 02)

This is the **densest** subsystem in Mercury — autonomous,
SQLite-backed, with merge / conflict / decay / consolidation /
reflection generation. The whole pattern transfers to CAQRS but with
research-domain memory types instead of user personality types.

## Layered architecture

```
┌──────────────────────────────────────┐
│ Agent (calls remember + retrieve)    │
└────────────────┬─────────────────────┘
                 │
┌────────────────▼─────────────────────┐
│ UserMemoryStore (business logic)     │
│  • remember(candidates)              │
│  • retrieveRelevant(query)           │
│  • consolidate() / prune()           │
│  • merge / conflict resolution       │
│  • health score / scope inference    │
└────────────────┬─────────────────────┘
                 │
┌────────────────▼─────────────────────┐
│ SecondBrainDB (storage)              │
│  • SQLite (better-sqlite3)           │
│  • FTS5 virtual table + triggers     │
│  • prepared statements (@-bound)     │
└──────────────────────────────────────┘
```

## SecondBrainDB — storage layer

### 1. Optional dependency probe (lines 11-29)

```ts
let syncDatabaseClass: typeof import('better-sqlite3') | null = null;
try {
  const mod = require('better-sqlite3');
  // Probe: open + close a tmp DB to confirm native binary works
  const probeDir = join(tmpdir(), `mercury-sqlite3-probe-${process.pid}`);
  mkdirSync(probeDir, { recursive: true });
  const probeDb = new mod(join(probeDir, 'probe.db'));
  probeDb.close();
  rmSync(probeDir, { recursive: true, force: true });
  syncDatabaseClass = mod;
} catch {
  syncDatabaseClass = null;
}
export function isBetterSqlite3Available(): boolean { return syncDatabaseClass !== null; }
```

This pattern is significant: the **module-level probe** ensures
`isBetterSqlite3Available()` is reliable. If the native binary
fails to load (missing build tools, wrong arch, etc.) the rest of
Mercury continues to work with `userMemory: null`. CAQRS analogue:
`sqlite3` is in Python's stdlib so no probe needed; but if FTS5 is
unavailable on a particular Python build, we should detect and fall
back. (Most Python distributions include FTS5 by default.)

### 2. SQLite schema (lines 78-130)

```sql
CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY,
  user_key TEXT NOT NULL,
  type TEXT NOT NULL,
  summary TEXT NOT NULL,
  detail TEXT,
  scope TEXT NOT NULL DEFAULT 'durable',
  evidence_kind TEXT NOT NULL DEFAULT 'inferred',
  source TEXT NOT NULL DEFAULT 'conversation',
  confidence REAL NOT NULL,
  importance REAL NOT NULL,
  durability REAL NOT NULL,
  evidence_count INTEGER NOT NULL DEFAULT 1,
  provenance TEXT,
  dismissed INTEGER NOT NULL DEFAULT 0,
  superseded_by TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  last_seen_at INTEGER NOT NULL,
  last_used_at INTEGER,
  last_used_query TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  summary, detail, content=memories, content_rowid=rowid
);

CREATE TABLE IF NOT EXISTS second_brain_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 5 indexes for the active query patterns
CREATE INDEX idx_memories_user_type    ON memories(user_key, type);
CREATE INDEX idx_memories_user_dismissed ON memories(user_key, dismissed);
CREATE INDEX idx_memories_user_updated   ON memories(user_key, updated_at);
CREATE INDEX idx_memories_user_scope     ON memories(user_key, scope);
CREATE INDEX idx_memories_user_evidence_kind ON memories(user_key, evidence_kind);

-- 3 FTS sync triggers
CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, summary, detail) VALUES (new.rowid, new.summary, new.detail);
END;
CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, summary, detail) VALUES('delete', old.rowid, old.summary, old.detail);
END;
CREATE TRIGGER memories_au AFTER UPDATE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, summary, detail) VALUES('delete', old.rowid, old.summary, old.detail);
  INSERT INTO memories_fts(rowid, summary, detail) VALUES (new.rowid, new.summary, new.detail);
END;
```

Pragmas: `journal_mode = WAL` (concurrent read/write), `synchronous =
NORMAL` (acceptable durability vs speed tradeoff), `foreign_keys = ON`.

The FTS5 virtual table indexes only `summary` + `detail`, with
`content=memories` so storage is non-redundant. Triggers keep FTS in
sync on INSERT/UPDATE/DELETE.

### 3. Soft-delete via `dismissed` flag

`dismissed = 1` removes a row from active queries (every SELECT has
`AND dismissed = 0`) but keeps it in the table for the FTS triggers
to operate on cleanly. `hardDeleteDismissed(userKey)` removes
permanently — called in `prune()`.

### 4. Search APIs (lines 244-270)

```ts
searchRelevant(userKey, query, limit = 10): MemoryRow[] {
  const tokens = query.split(/\s+/).filter(...).map(escapeQuotes);
  if (tokens.length === 0) {
    // Default: most-recently-updated active memories
    return /* SELECT ... ORDER BY updated_at DESC LIMIT ? */;
  }
  const ftsQuery = tokens.join(' OR ');
  try {
    return /* JOIN memories_fts MATCH ?, ORDER BY rank */;
  } catch {
    // FTS5 fallback: LIKE on every token
    return /* WHERE (summary LIKE ? OR detail LIKE ?) ... */;
  }
}
```

Two fallbacks: (a) when query has no useful tokens, return recency-
ordered active memories. (b) when FTS5 fails (e.g., reserved words,
syntax errors), fall back to LIKE-based search.

### 5. Targeted query helpers

- `getActive(userKey)`, `getById(id)`, `getByType(userKey, type)` —
  straightforward selects.
- `findMergeCandidate(userKey, type, normalizedTerms)` — searches for
  a memory of the same type whose summary contains the query terms,
  excluding negation-mismatches.
- `findConflictCandidate(userKey, type, summaryTerms)` — same shape
  but **prefers** negation-mismatches (i.e., looking for the
  *opposite* claim).
- `promoteToDurable(userKey)` — bulk update active memories with
  `evidence_count >= 3` to durable scope.
- `pruneStale(userKey)` — three-tier prune:
  - Active inferred > 21 days idle → dismissed.
  - Active direct > 42 days idle → dismissed.
  - Durable inferred > 120 days idle → confidence decayed by 0.15
    (floored at 0.15), then dismissed if confidence drops < 0.3.

### 6. Meta key/value table (lines 107-110, 344-358)

```sql
CREATE TABLE second_brain_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
```

Stores per-user-key aggregates that are expensive to compute on
every read:

- `<userKey>:profile_summary` — concatenated top-N memories by health
  score, refreshed by `consolidate()`.
- `<userKey>:active_summary` — recent goals/projects/decisions,
  refreshed by `consolidate()`.
- `<userKey>:learning_paused` — `'1'` / `'0'`.

This pattern matters: **derived data lives in a separate table from
primary data**, refreshed on schedule rather than on every read.

## UserMemoryStore — business logic

### 7. Memory types (lines 7-17)

```ts
type UserMemoryType =
  | 'identity'    // who the user is (durable)
  | 'preference'  // what they like (durable)
  | 'goal'        // what they're working toward (active, time-bound)
  | 'project'     // what they're building (active)
  | 'habit'       // recurring patterns (durable)
  | 'decision'    // choices made (active)
  | 'constraint'  // hard rules / limits (durable)
  | 'relationship' // people in their life (durable)
  | 'episode'     // specific events (active)
  | 'reflection'; // synthesized insights (durable, system-generated)
```

CAQRS analogue (research-domain):

```python
class ResearchMemoryType(StrEnum):
    HYPOTHESIS = "hypothesis"             # active until adopted/rejected
    REGIME = "regime"                     # active, time-windowed
    FAILED_HYPOTHESIS = "failed_hypothesis"  # durable, negative training
    ACCEPTED_STRATEGY = "accepted_strategy"  # durable
    CONSTRAINT = "constraint"             # durable, e.g., "do not trade through earnings"
    DATA_QUALITY_NOTE = "data_quality_note"  # durable, e.g., "FRED CPI data lags 30 days"
    EPISODE = "episode"                   # active, per-cycle event
    REFLECTION = "reflection"             # durable, synthesized
    PATTERN = "pattern"                   # durable, e.g., "momentum fails when VIX > 30"
    LESSON = "lesson"                     # durable, post-mortem learning
```

Same structural separation: durable vs active, system-generated vs
derived-from-conversation. The 10 types translate ~directly — the
labels change, the merge/conflict/decay logic does not.

### 8. Three scope/evidence dimensions

- **Scope**: `durable` (long-lived) vs `active` (time-bound). Inferred
  by `inferScope(candidate)`:
  - `goal/project/decision/episode` → active
  - others → durable
- **Evidence kind**: `direct` (user explicitly stated) vs `inferred`
  (model deduced) vs `manual` (user manually entered) vs `system`
  (consolidation-generated reflection).
- **Source**: `conversation` (extracted in turn) vs `system` (internal).

The triple is used for staleness rules and confidence adjustments
(see `effectiveConfidence` below).

### 9. `shouldStoreCandidate` filter (lines 470-476)

```ts
function shouldStoreCandidate(candidate): boolean {
  const summary = candidate.summary.trim();
  if (summary.length < 12 || summary.length > 220) return false;
  if (candidate.confidence < 0.55 /* MIN_CONFIDENCE */) return false;
  if (candidate.durability < 0.4 && candidate.importance < 0.7) return false;
  return true;
}
```

Three gates:

1. Summary length 12-220 chars.
2. Confidence ≥ 0.55.
3. Either durability ≥ 0.4 OR importance ≥ 0.7 (low-on-both is
   discarded).

Below all gates, the candidate is silently dropped. **No queue, no
review** — autonomous filter.

CAQRS analogue: research candidates need similar gating but with
domain-appropriate thresholds. E.g., a failed hypothesis with low
confidence (the model isn't sure it actually failed) and low
importance (low PnL impact) is discarded.

### 10. `remember(candidates)` — the merge/conflict pipeline (lines 170-203)

```ts
remember(candidates): UserMemoryRecord[] {
  if (this.isLearningPaused()) return [];
  for (const candidate of candidates) {
    if (!shouldStoreCandidate(candidate)) continue;

    // 1. MERGE: same-type with overlap >= 0.74 → strengthen evidence
    const mergeTarget = this.db.findMergeCandidate(...);
    if (mergeTarget && overlapScore(...) >= 0.74) {
      const merged = this.mergeRecord(mergeTarget, candidate);
      remembered.push(merged);
      continue;
    }

    // 2. CONFLICT: same-type with negation mismatch → resolve by confidence
    const conflictTarget = this.db.findConflictCandidate(...);
    if (conflictTarget) {
      const winner = this.resolveConflict(conflictTarget, candidate);
      if (winner === 'existing') continue;
    }

    // 3. INSERT: new memory
    this.insertRecord(candidate, source);
  }
  this.enforceMaxRecords();
}
```

Three-stage cascade: merge first (most common), then conflict
detection, finally insert. The conflict resolver is silent — higher
confidence wins; equal confidence → newer wins (existing dismissed).

### 11. `mergeRecord` — strengthen evidence (lines 336-354)

```ts
mergeRecord(existing, candidate): UserMemoryRecord {
  this.db.update({
    id: existing.id,
    summary: pickBetterSummary(existing.summary, candidate.summary),  // longer if ≤220
    detail: candidate.detail || existing.detail,
    confidence: max(existing.confidence, candidate.confidence),
    importance: max(existing.importance, candidate.importance),
    durability: max(existing.durability, candidate.durability),
    evidence_count: existing.evidence_count + 1,
    updated_at: now,
    last_seen_at: now,
  });
}
```

Merging **monotonically strengthens** all three numeric dimensions
(takes the max) and increments `evidence_count`. The summary is
replaced only if the incoming is longer (and within length limits).

### 12. `resolveConflict` (lines 356-393)

```ts
resolveConflict(existing, candidate): 'incoming' | 'existing' {
  if (incoming.confidence > existing.confidence) {
    // Soft-delete existing
    this.db.update({ id: existing.id, dismissed: 1, superseded_by: 'auto_resolved' });
    return 'incoming';   // proceed to insert
  }
  if (incoming.confidence < existing.confidence) {
    return 'existing';   // skip the insert
  }
  // Equal: newer wins (existing dismissed, incoming inserted)
  this.db.update({ id: existing.id, dismissed: 1, superseded_by: 'auto_resolved' });
  return 'incoming';
}
```

`superseded_by: 'auto_resolved'` is a marker for audit; the dismissed
row is preserved until `hardDeleteDismissed`.

### 13. `enforceMaxRecords` — health-score-based eviction (lines 395-411)

```ts
enforceMaxRecords(): void {
  const total = this.db.totalActive(this.userKey);
  if (total <= this.maxRecords) return;
  const allActive = this.db.getActive(this.userKey);
  const toDismiss = allActive
    .sort((a, b) => memoryHealthScore(b) - memoryHealthScore(a))
    .slice(this.maxRecords);
  for (const row of toDismiss) this.db.softDelete(row.id);
}
```

When over capacity, **dismiss the lowest-health-score records**.
Default `maxRecords = 50`.

### 14. `memoryHealthScore` (lines 489-497)

```ts
function memoryHealthScore(row): number {
  return (row.importance * 0.35)
    + (row.durability * 0.25)
    + (effectiveConfidence(row) * 0.25)
    + (Math.min(row.evidence_count, 5) / 5 * 0.15)
    + (row.scope === 'active' ? 0.08 : 0)
    - (row.superseded_by ? 0.3 : 0)
    - (isRowStale(row) ? 0.12 : 0);
}
```

Weighted sum: 35% importance, 25% durability, 25% effective
confidence, 15% evidence count (capped at 5). Bonuses/penalties:
+0.08 for active, −0.3 if superseded (sticky penalty even after auto-
resolve), −0.12 if stale.

### 15. `effectiveConfidence` — age + evidence-kind decay (lines 499-516)

```ts
function effectiveConfidence(row): number {
  const ageDays = (Date.now() - row.updated_at) / (1000 * 60 * 60 * 24);
  let confidence = row.confidence;
  if (row.evidence_kind === 'inferred') confidence -= min(0.2, ageDays / 365);
  else if (row.evidence_kind === 'manual') confidence += 0.06;
  else if (row.evidence_kind === 'direct') confidence += 0.03;
  if (row.scope === 'active') confidence -= min(0.18, ageDays / 120);
  return clamp(confidence, 0, 1);
}
```

Inferred memories decay (up to −0.2 over a year), manual memories
get a +0.06 trust bonus, direct +0.03. Active memories decay faster
(up to −0.18 over 120 days).

### 16. `isRowStale` (lines 518-527)

- Active scope: stale after 21 days.
- Durable + inferred: stale after 120 days.
- Durable + (direct/manual): stale after 365 days.

### 17. `retrieveRelevant` — prompt-injection retrieval (lines 116-168)

The "hot" read path that the agent calls before each LLM request.

```ts
retrieveRelevant(query, { maxRecords=5, maxChars=900 }): RetrievedUserMemory {
  const ftsResults = this.db.searchRelevant(this.userKey, query, max(maxRecords*2, 10));
  const ranked = this.scoreAndRank(ftsResults, query);
  // Pack top-K under maxChars budget
  // If empty, fall back to profile + active summary
  // Mark selected as 'last_used'
  return { records, context };
}
```

Scoring (line 425-443):

```
score = confidence*0.3 + importance*0.25 + durability*0.15
      + max(0, 0.2 - ageDays*0.005)               // recency boost
      + (matchTokens/queryTokens) * 0.1           // query overlap
```

Returns a `context` string ready to inject as a fake user/assistant
turn. Format:

```
User active state:
- <activeSummary>

User profile summary:
- <profileSummary>

Relevant user memory:
- [type] summary
- [type] summary
...
```

`maxChars=900` cap prevents runaway injection. `mark used` records
`last_used_at` and `last_used_query` so unused memories are
identifiable later.

### 18. `consolidate()` — periodic re-synthesis (lines 217-260)

Throttled to 5 minutes — only runs if `now - lastConsolidateAt >=
5min`. Triggered by the heartbeat (file 03 line 1024).

Three operations:

1. **Profile summary**: pick top scorer for each of identity,
   preference, goal, project, constraint, habit; concatenate up to 4;
   slice to 420 chars.
2. **Active summary**: filter recent (≤14 days) goal/project/decision/
   episode rows, top 3 by recency; slice to 360 chars.
3. **Reflections**: build candidates from grouped memory types:
   - 2+ preferences → reflection "User consistently shows these
     preferences: ..."
   - 2+ goals/projects → reflection "Current long-term direction: ..."
   - 2+ habits/constraints → reflection "Working style pattern: ..."

Reflections are stored as `type: reflection` with high confidence
(0.82-0.86) and durability (0.82-0.9), system-generated.

### 19. `prune()` — heartbeat maintenance (lines 262-270)

```ts
prune(): { activePruned, durablePruned, promoted } {
  const promoted = this.db.promoteToDurable(this.userKey);
  const { activePruned, durablePruned } = this.db.pruneStale(this.userKey);
  const hardDeleted = this.db.hardDeleteDismissed(this.userKey);
  return { activePruned, durablePruned, promoted };
}
```

Three actions in order: promote (active → durable for 3+ reinforced
direct/manual memories), prune stale, hard-delete dismissed. The
last step recovers disk space.

### 20. `hasConflict` — polarity detection (lines 572-604)

```ts
function hasConflict(a, b): boolean {
  const polarityPairs = [
    ['prefers', 'does not prefer'],
    ['likes', 'does not like'],
    ['wants', 'does not want'],
    ['is building', 'is not building'],
    ['uses', 'does not use'],
    ['enabled', 'disabled'],
  ];
  // Detect opposite polarity AND ≥0.5 overlap on the rest
  // ...
  // Generic negation marker fallback (not, never, no longer, avoid, against, disabled)
  // If one has negation and the other does not, AND ≥0.7 overlap → conflict
}
```

This is **NLP-lite**: hand-written polarity pairs + generic negation
detection with overlap thresholds. Imperfect but effective for
typical conversational text.

CAQRS analogue: research-domain conflicts have different polarity
pairs:

```python
research_polarity_pairs = [
    ("works on", "does not work on"),
    ("outperforms", "underperforms"),
    ("statistically significant", "not statistically significant"),
    ("passes acceptance", "fails acceptance"),
    ("regime active", "regime inactive"),
]
```

Plus signed-numeric-value conflicts that text-based detection cannot
catch ("Sharpe = 0.8" vs "Sharpe = 0.2") which CAQRS can handle by
also checking numeric metric values when memory types include
metrics.

## What CAQRS already has

Nothing.

## CAQRS implications

```
caqrs/memory/second_brain/
├── __init__.py
├── store.py              # ResearchMemoryStore (UserMemoryStore analogue)
├── db.py                 # SecondBrainDB analogue (sqlite3 stdlib)
├── types.py              # ResearchMemoryType enum, ResearchMemoryRecord pydantic
├── candidate.py          # ResearchMemoryCandidate + shouldStoreCandidate
├── merge.py              # mergeRecord + pickBetterSummary
├── conflict.py           # resolveConflict + hasConflict + research polarity pairs
├── score.py              # memoryHealthScore + effectiveConfidence + scoreAndRank
├── stale.py              # isRowStale + scope-specific cutoffs
├── consolidate.py        # buildProfileSummary + buildActiveSummary + buildReflectionCandidates
└── retrieve.py           # retrieveRelevant + format_for_prompt
```

This is a **substantial subsystem** — Mercury's user-memory.ts is
697 lines of dense logic. The Python port will likely be 800-1000
lines spread across modules. Estimated effort: P1.3 dedicated phase
(after agent layer in P1.2).

## Implementation priority for CAQRS

1. **MVP** (smallest useful slice):
   - `db.py` with sqlite3 + FTS5 + the schema (line-by-line port).
   - `store.py` with `remember()` (insert only, no merge/conflict
     yet) and `retrieveRelevant()` (FTS5 + scoreAndRank).
   - Tests with in-memory SQLite (`:memory:`).

2. **Add merge** — `findMergeCandidate` + `mergeRecord` +
   `overlapScore`. Tests for the 0.74 threshold.

3. **Add conflict** — `findConflictCandidate` + `hasConflict` +
   `resolveConflict`. Tests for polarity pairs.

4. **Add prune** — `pruneStale` + `promoteToDurable` +
   `hardDeleteDismissed`. Tests for 21/42/120/365 day cutoffs.

5. **Add consolidate** — `buildProfileSummary` + `buildActiveSummary`
   + `buildReflectionCandidates`. Tests for the throttle.

6. **Wire into orchestrator heartbeat** — call `prune()` and
   `consolidate()` periodically.

7. **Wire into agent prompt build** — call `retrieveRelevant()` at
   the start of each agent invocation.

Each step is independently testable with synthetic candidates and
in-memory SQLite, so the subsystem can be built incrementally
without blocking other work.

## Open questions

- Mercury keeps `maxRecords = 50` per user. CAQRS may need much
  larger capacity for accumulated research memory (thousands of
  failed hypotheses across years). Calibrate at P1.3 (e.g., 1000
  per `user_key`, with `user_key` as `research:<universe-id>`).
- Mercury's polarity pairs are English text. CAQRS research
  memories may be more structured (numeric metric values, dates).
  The conflict detector should be **type-aware**: numeric memories
  conflict on value, text memories conflict on polarity.
- The `consolidate()` reflection generator hardcodes which memory
  types contribute (preferences, goals/projects, habits/constraints).
  CAQRS analogues should generate research-domain reflections (e.g.,
  "Pattern: momentum strategies fail when realized vol > 30%" from
  multiple failed-hypothesis memories pointing to high-vol regimes).
  Defer the reflection logic to P2 once enough cycles have run.
- Mercury's `userKey` is `'user:owner'`. CAQRS could use
  `'research:<run_id>'` for per-cycle memory or `'research:global'`
  for cross-cycle memory. **Both** — separate stores at different
  retention horizons.
