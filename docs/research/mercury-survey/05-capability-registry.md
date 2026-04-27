# 05 — Capability Registry (`src/capabilities/registry.ts`)

## Mercury source

- File: `src/capabilities/registry.ts`
- Lines: 191 (complete read).

## Key types

```ts
interface ChatCommandContext {
  toolNames: () => string[];
  skillNames: () => string[];
  config: () => MercuryConfig;
  tokenBudget: () => TokenBudget;
  manual: () => string;
  memorySummary, memoryRecent, memorySearch,
  memorySetLearningPaused, memoryClear,
}

class CapabilityRegistry {
  permissions: PermissionManager;
  private tools: Record<string, Tool>;
  private skillLoader, scheduler, tokenBudget, sendFileHandler, sendMessageHandler;
  private currentChannelId, currentChannelType, currentCwd;
  private chatCommandContext;
}
```

## Implementation patterns

### 1. Conditional tool registration (lines 106-178)

`registerAll()` is called once at startup. Each block is gated by
either a config flag or the presence of an injected handler:

```ts
if (manifest.capabilities.filesystem.enabled) {
  this.tools.read_file = createReadFileTool(this.permissions, () => this.getCwd());
  this.tools.write_file = createWriteFileTool(...);
  // ...
}

if (this.sendMessageHandler) {
  this.tools.send_message = createSendMessageTool(this.sendMessageHandler);
}

if (manifest.capabilities.shell.enabled) {
  this.tools.run_command = createRunCommandTool(this.permissions, getCwd, setCwd);
  this.tools.cd = createCdTool(getCwd, setCwd);
  this.tools.approve_command = createApproveCommandTool(this.permissions);
}

if (this.skillLoader) { ... }
if (this.scheduler)   { ... }
if (this.tokenBudget) { ... }
if (manifest.capabilities.git?.enabled) { ... }
if (isGitHubConfigured())               { ... }
this.tools.fetch_url = createFetchUrlTool(); // always registered
```

The pattern: **dependency injection via constructor + factories that
close over the dependencies they need.** Each tool factory takes
exactly the resources it needs (PermissionManager, getCwd, etc.) and
returns a `Tool` object.

### 2. Closure-based context binding

Every factory receives `() => this.getCwd()` (a getter, not the value)
so that subsequent `setCwd()` calls inside one tool are visible to
other tools. Channel context (`channelId`, `channelType`) follows
the same pattern via `setChannelContext` + `getChannelContext`.

CAQRS analogue: agents receive an `OrchestratorContext` providing
`current_cycle_id`, `current_run_metadata`, `current_budget`, etc.
via getters, not snapshots.

### 3. Tool list categories

Filesystem tools: `read_file, write_file, create_file, list_dir,
delete_file, edit_file, send_file, approve_scope`.

Shell tools: `run_command, cd, approve_command`.

Skill tools: `install_skill, list_skills, use_skill`.

Scheduler tools: `schedule_task, list_scheduled_tasks,
cancel_scheduled_task`.

Git tools: `git_status, git_diff, git_log, git_add, git_commit,
git_push`.

GitHub tools (only if `isGitHubConfigured()`): `create_pr, review_pr,
list_issues, create_issue, github_api`.

System tools: `budget_status`.

Web tools: `fetch_url` (always present).

Messaging: `send_message`, `send_file` (handler-gated).

CAQRS tool inventory (P1.2+):

- **Data tools** (data-source readers): `fetch_prices`,
  `fetch_news`, `fetch_macro_series`, `fetch_filings`,
  `fetch_social_sentiment`.
- **Research tools**: `run_backtest`, `simulate_walk_forward`,
  `compute_regime`, `evaluate_acceptance_criteria`.
- **Execution tools** (P3+): `simulate_trade`, `submit_order`,
  `cancel_order`, `query_position`.
- **Meta tools**: `emit_artifact`, `query_memory`, `record_lesson`.

### 4. ChatCommandContext as agent ↔ registry bridge

The agent installs a `ChatCommandContext` into the registry so that
chat commands (`/status`, `/memory`, `/tools`, `/skills`,
`/budget`, `/telegram`) can introspect agent state without the
agent needing to re-implement command logic in the registry. The
context is a struct of getters — read-only views of agent state.

CAQRS analogue: an `OrchestratorIntrospection` struct exposing
current cycle, recent artifacts, budget, etc. for both internal
introspection and any future CLI commands.

### 5. Single source of truth: `getTools(): Record<string, Tool>`

The registry is the **only** place tools are constructed. The agent
loop fetches `this.capabilities.getTools()` and passes the result
directly to the Vercel AI SDK. **The agent never knows what tools
exist** — it only knows the registry and the resulting record.

This means tool registration order does not matter, and adding a new
tool requires only one change (its factory call inside `registerAll`).

CAQRS analogue: the orchestrator instantiates a `ToolRegistry` once,
populates it conditionally based on credentials / config, and each
agent receives the (filtered) tool subset it is allowed to use.

## What CAQRS already has

Nothing yet at this layer — the providers/ module is the closest
analogue (a registry of LLMProvider instances), but tools are a
separate axis.

## CAQRS implications

```
caqrs/tools/
├── registry.py          # ToolRegistry — central tool factory dispatch
├── base.py              # Tool protocol (input pydantic / output pydantic)
├── data/
│   ├── prices.py
│   ├── news.py
│   ├── macro.py
│   └── ...
├── research/
│   ├── backtest.py
│   └── walk_forward.py
└── meta/
    ├── emit_artifact.py
    └── query_memory.py
```

Tool protocol shape:

```python
class Tool(Protocol):
    name: str
    description: str
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    async def run(self, input: BaseModel, *, ctx: OrchestratorContext) -> BaseModel: ...
```

Registration pattern:

```python
class ToolRegistry:
    def __init__(self, *, policy_gateway, market_client, broker_client | None) -> None: ...
    def register_all(self) -> None:
        if self._market_client:
            self._tools["fetch_prices"] = FetchPricesTool(self._market_client)
        if self._broker_client:
            self._tools["submit_order"] = SubmitOrderTool(self._broker_client, self._policy_gateway)
        # ...
```

Each agent then declares its allowed tools (e.g., Observer Agent ⊂
data tools; Research Agent ⊂ data + research tools; Execution Agent ⊂
execution tools). The orchestrator filters before passing to the LLM.

## Open questions

- Should each agent have its own tool subset declared in code, or
  should it be a config / declarative manifest? **Lean toward code**
  for P1.2 (typed, refactorable); revisit if the matrix gets large.
- Do we need the equivalent of Mercury's `chat command` system for
  CAQRS? **Probably not in P1**. Research cycles are launched by
  explicit operator command, not by chat. /status and /memory
  introspection can come later.
