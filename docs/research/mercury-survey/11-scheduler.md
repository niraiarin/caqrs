# 11 — Scheduler (`src/core/scheduler.ts`)

## Mercury source

- File: `src/core/scheduler.ts`
- Lines: 212 (complete read).

## Key types

```ts
interface ScheduledTask        { id; cron; handler; description; }
interface ScheduledTaskManifest { id; cron?; description; skillName?; prompt?;
                                  delaySeconds?; executeAt?; createdAt;
                                  sourceChannelId?; sourceChannelType?; }

class Scheduler {
  tasks: Map<string, cron.ScheduledTask>;          // node-cron handles
  delayedTasks: Map<string, NodeJS.Timeout>;        // setTimeout handles
  taskManifests: Map<string, ScheduledTaskManifest>; // canonical state
  heartbeatTimer: NodeJS.Timeout | null;
}

function loadSchedules(): ScheduledTaskManifest[];
function saveSchedules(tasks): void;
```

Persistence: `~/.mercury/schedules.yaml` with shape `{ tasks: [...] }`.

## Implementation patterns

### 1. Two scheduling primitives in one class

- **Cron tasks** (`addPersistedTask`, `addTask`): repeating, expression-
  based, persisted.
- **Delayed tasks** (`addDelayedTask`): one-shot via `setTimeout`,
  persisted with `executeAt` so restarts can re-schedule.
- **Heartbeat** (`startHeartbeat`, `stopHeartbeat`): a single
  `setInterval` with a configurable cadence (`config.heartbeat.intervalMinutes`).

All three share the same `taskManifests` map for restart recovery.

### 2. Restart recovery (lines 173-195)

```ts
restorePersistedTasks(): void {
  const persisted = loadSchedules();
  for (const manifest of persisted) {
    if (manifest.delaySeconds) {
      const executeAt = manifest.executeAt ? new Date(manifest.executeAt) : null;
      const now = Date.now();
      if (executeAt && executeAt.getTime() > now) {
        const remainingMs = executeAt.getTime() - now;
        manifest.delaySeconds = Math.ceil(remainingMs / 1000);
        this.addDelayedTask(manifest);
      } else {
        logger.info(...'Delayed task already expired, skipping');
      }
    } else if (manifest.cron && cron.validate(manifest.cron)) {
      this.addPersistedTask(manifest);
    } else {
      logger.warn(...'Skipping invalid task');
    }
  }
}
```

Three-way disposition on restart:

- Future-dated delayed task: re-schedule with remaining time.
- Past-dated delayed task: silently discard (already missed).
- Cron task with valid expression: re-schedule.
- Cron task with invalid expression: discard with warning.

CAQRS analogue: research cycles have **no inherent recurrence** —
they are launched explicitly. Mercury-style recurring tasks are
out of scope for P1. **However**, if CAQRS later adds "run daily
hypothesis review at 7am UTC", the same pattern applies.

### 3. Handler injection via setter (lines 67, 72-74)

```ts
constructor(config, private onScheduledTask?) { ... }
setOnScheduledTask(handler) { this.onScheduledTask = handler; }
```

The Agent class wires this up in its constructor:

```ts
this.scheduler.setOnScheduledTask(async (manifest) => this.handleScheduledTask(manifest));
```

This separates **scheduler mechanism** (timers, persistence) from
**task semantics** (what to actually do when a task fires). The
scheduler does not import the agent.

CAQRS analogue: same pattern. `Orchestrator` injects its cycle-launch
callback into a `Scheduler` instance.

### 4. Heartbeat as a separate concern (lines 76-100)

```ts
onHeartbeat(handler) { this.heartbeatHandler = handler; }
startHeartbeat() {
  if (this.heartbeatTimer) return;  // idempotent
  const ms = this.heartbeatIntervalMinutes * 60 * 1000;
  this.heartbeatTimer = setInterval(async () => {
    try { await this.heartbeatHandler?.(); }
    catch (err) { logger.error(...'Heartbeat error'); }
  }, ms);
}
```

The heartbeat is **not** a cron task. It is a fixed interval timer
with its own handler. This is a deliberate split:

- Cron tasks may fire never, daily, hourly — driven by user
  configuration.
- Heartbeat fires continuously at a small interval (default likely
  ~5 minutes from `config.heartbeat.intervalMinutes`).

The heartbeat handler does proactive maintenance (file 03 lines
1024-1078): episodic prune, second brain consolidate, budget
notifications.

CAQRS implication: include a heartbeat hook even if P1 does not yet
populate it. Future use cases:
- Stale hypothesis pruning
- Regime fingerprint refresh
- Per-cycle cost report digest

### 5. node-cron specifics

```ts
import cron from 'node-cron';
const scheduled = cron.schedule(task.cron, async () => { await task.handler(); });
cron.validate(manifest.cron);
```

`node-cron` is the dependency. CAQRS analogue: `croniter` (Python
package) for cron parsing + scheduling. Or APScheduler / Celery beat
if heavier needs. **Recommend croniter only** for P1; the heavy
schedulers add operational complexity disproportionate to research-
prototype needs.

### 6. `stopAll()` cleanup (lines 201-212)

```ts
stopAll(): void {
  this.stopHeartbeat();
  for (const [, task] of this.tasks) task.stop();
  for (const [, timer] of this.delayedTasks) clearTimeout(timer);
  this.tasks.clear();
  this.delayedTasks.clear();
  this.taskManifests.clear();
}
```

Called by the agent's `sleep()` / `shutdown()` paths. **Does not
persist before clearing** — stopping is graceful but not "save state
and resume later". Persistent state is only `~/.mercury/schedules.yaml`,
written by `persistSchedules()`.

CAQRS analogue: orchestrator shutdown should persist any in-flight
cycle state to `~/.caqrs/state.yaml` so a restart can resume,
**not** just clear in-memory state. This is an upgrade over Mercury's
behaviour (Mercury restarts re-load from `schedules.yaml` only —
in-flight scheduled-task execution state is not preserved).

## What CAQRS already has

Nothing.

## CAQRS implications

```python
# caqrs/orchestrator/scheduler.py
import asyncio
from collections.abc import Callable, Awaitable
from croniter import croniter

class Scheduler:
    def __init__(self, *, on_cycle_fire: Callable[[CycleManifest], Awaitable[None]] | None = None) -> None: ...
    def schedule_cron(self, manifest: CycleManifest) -> None: ...
    def schedule_delayed(self, manifest: CycleManifest, *, delay_seconds: int) -> None: ...
    def restore_persisted(self) -> None: ...
    def stop_all(self) -> None: ...
    def on_heartbeat(self, handler: Callable[[], Awaitable[None]]) -> None: ...
    def start_heartbeat(self, *, interval_seconds: int = 300) -> None: ...
```

Scope for P1.4 (orchestrator). For P1.2 (Agent layer), the scheduler
is not yet needed — cycles are launched by direct CLI invocation.

## Open questions

- Should CAQRS persist in-flight cycle state (artifacts emitted so
  far + last successful agent in pipeline) so a restart can resume
  mid-cycle? **Yes**, but the schema needs to be designed first.
  Defer to P1.4 with a clear ADR.
- Mercury uses `node-cron`. CAQRS could use `croniter` (just parse +
  next-fire times) and roll its own asyncio-based dispatch — avoids
  pulling in a heavier scheduler. Recommend this approach.
- What is CAQRS's heartbeat interval? Mercury's likely 5-15 minutes.
  CAQRS heartbeat should be **research-cycle-aware**: fire only when
  IDLE so it does not interrupt an active cycle.
