# 03 — Agent Harness (`src/core/agent.ts`)

## Mercury source

- File: `src/core/agent.ts`
- Lines surveyed: **1-1890** (complete)
- Lines of substance: ~1,500 (rest is whitespace and Telegram-management
  CLI menus that are out of scope for CAQRS)

## Key types and entry points

```ts
class ToolCallLoopDetector { /* lines 34-240 */ }
class Agent { /* lines 244-1890 */ }
```

`Agent` is the singleton orchestrator. It owns:

- `lifecycle: Lifecycle` — state machine (file 10)
- `scheduler: Scheduler` — cron heartbeat + scheduled tasks (file 11)
- `capabilities: CapabilityRegistry` — tools + permissions (file 04, 05)
- `providers: ProviderRegistry` — LLM provider fallback chain (file 12)
- `identity: Identity` — soul / persona / guardrails (file 06)
- `shortTerm`, `longTerm`, `episodic: ...Memory` — flat-file memory (file 07)
- `userMemory: UserMemoryStore | null` — second brain (file 08)
- `channels: ChannelRegistry` — CLI / Telegram I/O
- `tokenBudget: TokenBudget` — daily token cap

Public lifecycle hooks: `birth() / wake() / sleep() / shutdown()`.

Public message entry: messages flow in via channel callbacks
(`channels.onIncomingMessage(...)`) into a queue
(`messageQueue: ChannelMessage[]`) processed sequentially by
`processQueue()`.

## Implementation patterns (named)

### 1. ToolCallLoopDetector — multi-axis loop detection

Implementation: lines 34-240.

State per detector instance:

- `recentCalls: { tool, params, failed }[]` — sliding window of 30 most
  recent tool calls.
- `totalCalls: number` — absolute counter.
- `recentStepTexts: string[]` — last 12 normalised assistant texts.
- `consecutiveNoActionSteps: number` — counter for "thought without
  action" steps.
- `hardAborted: boolean` — sticky flag: once true, generation is
  considered terminated.

Detection rules in priority order:

| Rule | Trigger | Action |
| --- | --- | --- |
| `detectAbsoluteLimit` | `totalCalls >= 25` OR failed >= 12 | hard abort |
| `detectIdentical` | last 3 calls have identical `tool` + `params` | hard abort |
| `detectSimilarLoop` | last 4 calls are same tool, all failing | hard abort |
| `detectSameTool` (soft) | same tool consecutively N times where N is 5 for high-tolerance tools (`fetch_url, read_file, list_dir, web_search, github_api`), 3 otherwise; threshold drops further if 3+ failures detected | warn + ask user to continue |
| `detectTextRepetition` | last 3 step texts have ≥0.7 jaccard similarity | warn |
| `recordNoActionResult` | 5 consecutive steps with no tool call AND no text | abort |

Auxiliary:

- `record(tool, params, failed)` — appends to window, trims to 30.
  `params` is `JSON.stringify(...).slice(0, 200)` so deeply nested
  payloads do not bloat the window.
- `recordStepText(text)` — keeps last 12 normalised texts (lowercased,
  whitespace-collapsed, sliced to 200 chars).
- `reset()` — invoked when entering a `use_skill` call (skills are
  expected to do their own multi-step work).
- `isHardAborted()` — read from outside to distinguish "loop abort"
  from "provider failure" in the fallback iterator.

The class is **stateful and instance-scoped** — a fresh detector per
`handleMessage` call. State is not persisted.

### 2. Pre-LLM warning injection (lines 414-458)

Before sending the prompt to the LLM, scan the last 6 messages from
short-term memory and look for:

- Three consecutive assistant turns that all `[Using: <tool>]` the same
  tool → inject a system warning telling the LLM to stop and try a
  different approach (the warning even gives concrete advice for
  common cases like git push auth failures, suggesting `github_api`).
- Three assistant turns with text overlap >0.75 → inject a warning
  about repetitive responses.

The warning is injected as a fake `user` message with an
acknowledging `assistant` reply, so the LLM sees the loop pattern
and the acknowledgement in conversation history. **This complements
the reactive `ToolCallLoopDetector`** — together they act both before
and during generation.

### 3. Provider fallback iterator (lines 501-850)

```ts
const fallbackIterator = this.providers.getFallbackIterator();
for (const provider of fallbackIterator) {
  try { result = await streamText(...) | generateText(...); break; }
  catch (err) {
    if (loopDetector.isHardAborted()) {
      // Treat partial response as success, do not try next provider
      result = result || { text: streamedText || fallback_message };
      break;
    }
    // Otherwise try next provider
    lastError = err;
  }
}
```

Two crucial behaviours:

- **Loop abort short-circuits the fallback chain**: if the loop
  detector aborted generation, that is *not* a provider failure;
  trying another provider would just hit the same loop. The current
  partial response (or a canned "I detected I was stuck" message) is
  used.
- **Last-success caching**: `providers.markSuccess(name)` after a
  successful generation. Future calls bias toward that provider
  (already mirrored in CAQRS `ProviderRegistry`).

### 4. `streamText` vs `generateText` symmetric duplication (lines 525-825)

Two ~150-line near-duplicate blocks:

- `streamText` path when `canStream` (CLI always; Telegram only if
  `telegramStreaming` flag enabled).
- `generateText` path otherwise.

Both register the same `onStepFinish` callback. The duplication is a
pragmatic choice — the streaming machinery (`textStream`,
`sendStreamToChat`, channel-specific stream handlers) needs to be
wired up in only one branch, but the loop-detection logic must run on
both paths identically.

A Python port should consolidate these into one inner function that
accepts a "consume the response" callable, since httpx-async streaming
(SSE) and non-streaming responses share the same payload shape.

### 5. `onStepFinish` as the hot path (lines 534-657 / 699-823)

Every tool call's success/failure is detected by string-matching the
result text against well-known failure markers:

```ts
const resultStr = typeof tr?.result === 'string'
  ? tr.result
  : JSON.stringify(tr?.result ?? '');
const failed = resultStr.length < 5000 && (
  resultStr.startsWith('Error:') ||
  resultStr.startsWith('⚠') ||
  resultStr.includes('exited with code') ||
  resultStr.includes('Command failed') ||
  resultStr.startsWith('Command exited with code')
);
```

This is fragile (a tool that legitimately returns a string starting
with `Error:` would be mis-classified) but **pragmatic for an
untyped tool result**. CAQRS does not have this problem because tool
results are typed pydantic models, so a `failed: bool` discriminator
can live on the tool-result type itself.

After detection, the callback runs the priority cascade
(`detectAbsoluteLimit` → `detectIdentical` → `detectSimilarLoop` →
`detectSameTool` → `detectTextRepetition`) and aborts via
`AbortController` when a hard rule fires.

### 6. `use_skill` resets the detector (lines 559-561 / 724-726)

```ts
if (toolCalls.some((tc: any) => tc.toolName === 'use_skill')) {
  loopDetector.reset();
}
```

Skills are expected to do bounded multi-step work of their own (read a
file, run a command, write output). Counting those toward the parent
loop budget would falsely trigger loop detection. So when a skill is
invoked, the detector resets. **CAQRS analogue**: when an agent
invokes a sub-agent (e.g., Hypothesis → Skeptic), the parent
orchestrator's call counter should reset for the sub-agent's budget.

### 7. `auto-approve-all` mode resets soft-loops (lines 584-585 / 749-750)

For internal/scheduled tasks, the soft-loop ask-the-user prompt is
nonsensical (no human present). The detector is reset and the prompt
skipped. **CAQRS analogue**: when CAQRS runs in headless mode (cron-
triggered cycle), the equivalent "ask user to continue?" path becomes
"silent log + continue" or "log + abort with clear error", configurable
per deployment.

### 8. `askToContinue` UX (lines 588-600 / 752-765)

When a soft loop fires interactively, the channel is asked to render
a yes/no prompt to the user. If yes, the detector is reset and the
LLM continues. If no, generation is aborted via the `AbortController`.

This is a **second-chance pattern**: not all soft loops are real (the
LLM may legitimately need to call the same tool many times when
exploring a directory tree). Letting the human resolve ambiguity is
cheaper than over-aborting.

### 9. Memory injection at prompt-build time (lines 460-487)

Before the LLM call, the harness pre-loads relevant memory:

```ts
if (this.userMemory) {
  const memoryContext = this.userMemory.retrieveRelevant(
    msg.content, { maxRecords: 5, maxChars: 900 });
  if (memoryContext.context) {
    messages.push({ role: 'user', content: memoryContext.context });
    messages.push({ role: 'assistant', content: 'Noted...' });
  }
} else {
  // Fallback to LongTermMemory keyword search
  const relevantFacts = this.longTerm.search(msg.content, 3);
  if (relevantFacts.length > 0) { /* inject */ }
}
```

The injection is **not a tool call** — it is a fake user/assistant
turn pair preceding the real user message. This keeps memory access
off the agentic loop's call budget. **Cap of 900 chars** prevents
runaway memory injection when the second brain has thousands of
records.

### 10. `buildSystemPrompt` composition (lines 925-981)

Always-on baseline (~500 tokens):

1. `identity.getSystemPrompt(config.identity)` — soul + guardrails +
   persona with `{name}` / `{owner}` / `{creator}` substituted.
2. `capabilities.getSkillContext()` — bullet list of installed skills
   (name + description only, progressive disclosure).
3. `tokenBudget.getStatusText()` — current usage line.
4. Conditional: `"Be concise to conserve tokens."` when usage > 70%.
5. Environment block: platform + cwd.
6. Second-brain status block: enabled/disabled, total memory count,
   learning paused state.
7. Conditional: GitHub hint block when GitHub tools are registered
   (default repo, tool descriptions, fall-back from `git_push` to
   `github_api`).

The prompt is composed afresh on every message. CAQRS analogue: **per
agent role**, build a small system prompt with role description +
schema name; do not maintain a multi-file persona model.

### 11. `extractMemory` background fire-and-forget (lines 1080-1163)

After a successful response, in a **non-awaited** `catch`-handled call:

```ts
this.extractMemory(msg.content, finalText).catch(err => {
  logger.warn({ err }, 'Memory extraction failed');
});
```

Inside `extractMemory`:

- Skip on trivial messages (`hi/hello/thanks/...` regex).
- Skip if token budget cannot afford ~800 tokens.
- Use `providers.getDefault()` (not the fallback chain) — extraction
  is best-effort, not user-facing.
- System prompt asks for **JSON array of memory candidates** with
  `type / summary (12-220 chars) / detail / evidenceKind / confidence
  / importance / durability`. Markdown code fences are stripped.
- **Fallback parsing**: if JSON parse fails, treat each non-empty
  bullet line as a `preference`-typed candidate with default scores.
- Validate types against the closed enum. Clamp confidence /
  importance / durability to `[0, 1]`. Drop summaries outside
  `[12, 220]` chars.
- Hand off to `userMemory.remember(typed, 'conversation')`. The merge
  / conflict / tier / decay logic lives downstream (file 08).

The user **never waits** for this. The loop is already idle by the
time extraction starts.

### 12. `heartbeat` proactive maintenance (lines 1024-1078)

Triggered by `Scheduler.onHeartbeat`. Per tick:

- **Episodic prune**: drop events older than 7 days that lack
  `metadata.important`.
- **Second brain consolidate**: re-synthesise the profile and active
  summaries; generate reflections from reinforced memories.
- **Second brain prune**: drop stale active memories (21 days), decay
  durable inferred memories with no reinforcement in 120 days, dismiss
  below 0.3 confidence.
- **Notification surface**: if budget usage ≥ 80%, push a notification
  to the notification channel; if any scheduled task fires within 5
  minutes, push a heads-up.

This is the "agent thinks while idle" loop. CAQRS analogue:
research-cycle heartbeat could prune stale hypothesis cards, decay
regime fingerprints, surface notifications to the orchestrator log.

### 13. `processInternalPrompt` — agents fire their own messages (lines 983-993)

```ts
async processInternalPrompt(prompt, channelId?, channelType?) {
  const syntheticMsg = { senderId: 'system', channelType: 'internal' | ..., ... };
  this.enqueueMessage(syntheticMsg);
}
```

Used by scheduled tasks. The synthetic message goes through the same
queue as user messages. **Internal/scheduled** messages enable
`auto-approve-all` for the duration (lines 336-341, finally clause
line 918-920).

CAQRS analogue: orchestrator can fire a follow-up cycle (e.g., after
a successful Hypothesis adoption, automatically launch the Research
Agent for the same hypothesis). Same queue, same loop, just different
provenance flag.

### 14. Sequential queue with reentrancy guard (lines 280-303)

```ts
private async processQueue(): Promise<void> {
  if (this.processing) return;
  if (this.messageQueue.length === 0) return;
  if (!this.lifecycle.is('idle')) return;
  this.processing = true;
  while (this.messageQueue.length > 0) {
    const msg = this.messageQueue.shift()!;
    try { await this.handleMessage(msg); }
    catch (err) { logger.error({ err, ... }); }
  }
  this.processing = false;
}
```

- Strictly sequential — only one message in flight at a time.
- Reentrancy guarded by `this.processing` flag.
- Lifecycle gate: `idle` is required to start.
- Per-message error handling — one bad message does not poison the
  queue.

CAQRS analogue: orchestrator processes one cycle at a time. Multiple
hypotheses can sit in queue but research runs are serial. Parallel
execution is opt-in and lives outside this loop.

### 15. Token budget enforcement at the entry edge (lines 393-412)

Before the LLM call, if `tokenBudget.isOverBudget()`, the harness
short-circuits: render a "you're over budget" message, present
override options (1=override / 2=reset / 3=set / 4=cancel), and return
to idle without calling the LLM. **This protects against runaway
costs on a session where the budget was tripped earlier.**

CAQRS analogue: per-experiment budget at the orchestrator. Each
research cycle has a cap; exceeding it pauses the cycle and surfaces
to the operator log.

### 16. Channel-aware streaming (lines 510-515, 663-679)

`canStream = (channelType === 'cli') || (channelType === 'telegram'
&& telegramStreaming)`. The Telegram streaming flag is runtime-
toggleable via `/stream on|off`, defaulting from config. Telegram has
a special `sendStreamToChat(chatId, textStream)` adapter that handles
its rate limits.

CAQRS skips this entirely — research outputs are atomic artifacts, not
chat streams.

### 17. Per-tool feedback to channel (lines 601-632 / 766-797)

For each tool call, the channel receives:

- `sendToolFeedback(toolName, params)` — visual indicator that the
  tool is running.
- `sendStepDone(toolName, result)` — visual indicator that it
  completed (and the result, often truncated).

Channel-specific implementations (`CLIChannel.sendStepDone`,
`TelegramChannel.sendStepDone`) handle formatting. The agent loop
itself emits identical events; only the rendering varies.

CAQRS analogue: **structured event log** rather than per-channel
rendering. The orchestrator emits events
(`hypothesis_emitted`, `backtest_started`, ...) and any consumer
(stdout logger, file, future Mercury skill) renders them.

## What CAQRS already has

- `ProviderRegistry` with ordered fallback + last-success bias
  (P1.0). Mirrors lines 501-850 partially.
- `RunMetadata` carrying token / latency / cost (P0). Mirrors the
  per-step usage tracking but at artifact level rather than per LLM
  call.

## What CAQRS does NOT have (and Mercury demonstrates)

The following are concrete patterns to port, with priority for P1.2 /
P1.3 / P1.4:

| # | Pattern | CAQRS module (proposed) | Phase |
| --- | --- | --- | --- |
| 1 | `ToolCallLoopDetector` | `caqrs/orchestrator/loop_detector.py` | **P1.2** |
| 2 | Pre-LLM warning injection | `caqrs/orchestrator/preflight.py` | **P1.2** |
| 3 | Provider fallback iterator | already in `caqrs/providers/registry.py` | done |
| 4 | streamText/generateText symmetric path | use single httpx async path | n/a |
| 5 | `onStepFinish` hot path | replaced by typed agent results | **P1.2** |
| 6 | `use_skill` reset | sub-agent reset hook in orchestrator | P1.2 |
| 7 | Headless `auto-approve-all` mode | orchestrator config flag | P1.2 |
| 8 | `askToContinue` second-chance | optional human-in-the-loop hook | P1.2 (later) |
| 9 | Memory injection at prompt-build | `caqrs/memory/retrieve.py` | **P1.3** |
| 10 | `buildSystemPrompt` composition | per-agent prompt builder | P1.2 |
| 11 | `extractMemory` background | `caqrs/memory/extractor.py` | P1.3 |
| 12 | `heartbeat` proactive maintenance | `caqrs/orchestrator/heartbeat.py` | P1.4 (or skip until needed) |
| 13 | `processInternalPrompt` self-fire | `Orchestrator.fire_internal_cycle` | P1.4 |
| 14 | Sequential queue | `caqrs/orchestrator/queue.py` | P1.4 |
| 15 | Token budget at entry edge | `caqrs/orchestrator/budget.py` | P1.4 |
| 16 | Channel-aware streaming | skip | n/a |
| 17 | Per-tool feedback events | structured event log | P1.4 |

## Open questions

- Mercury keeps `recentCalls` to a 30-element window. CAQRS may want a
  **larger window per agent** (research agents can legitimately call
  many tools), but a **smaller per-cycle absolute cap** (research
  cycles should not run for hours uninterrupted). Calibrate during
  P1.2 implementation.
- Mercury's `failed` heuristic is brittle. CAQRS gets this for free
  because tool returns are typed (a `BacktestReport.error_code` or
  similar discriminator can live on the result type).
- Mercury injects `[SYSTEM WARNING]` as a real chat turn — the LLM
  sees it and can be addressed by it. CAQRS agents don't have multi-
  turn context with the LLM; the loop detector aborts at the
  orchestrator level, not by talking to the model. **This is a
  significant philosophical difference**: Mercury negotiates with the
  model in-band; CAQRS imposes hard limits out-of-band.

## CAQRS implications (summary)

The single biggest takeaway: **almost every responsibility in the
1500-line `agent.ts` corresponds to a separate ~100-200 line module
in CAQRS**. The Python port should not become a 1500-line file. Each
named pattern above is a candidate Python module. Tests should pin
each pattern's behaviour independently — Mercury's tests for
`ToolCallLoopDetector` (not surveyed yet but presumably exist as
`agent.test.ts` or similar) would each become its own pytest file in
CAQRS.
