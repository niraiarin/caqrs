# 04 — Permission System (`src/capabilities/permissions.ts`)

## Mercury source

- File: `src/capabilities/permissions.ts`
- Lines: 489 (complete read).

## Key types

```ts
interface FileScope { path; read; write; }
interface FsPermissions { enabled; scopes: FileScope[]; }
interface ShellPermissions { enabled; blocked: string[]; autoApproved: string[]; needsApproval: string[]; cwdOnly; }
interface GitPermissions { enabled; autoApproveRead; approveWrite; }
interface PermissionsManifest { capabilities: { filesystem; shell; git; } }

class PermissionManager { /* ~330 lines of methods */ }
```

Manifest persisted at `~/.mercury/permissions.yaml` and loaded once
on construction. Defaults seed the manifest if the file is missing.

## Implementation patterns

### 1. Three-tier shell rules

```ts
blocked:        // never executed, even with approval
  ['sudo *', 'rm -rf /', 'rm -rf ~', 'mkfs *', 'dd if=*',
   ':(){ :|:& };:', 'shutdown *', 'reboot *', 'kill -9 1', ...]
autoApproved:   // run silently
  ['ls *', 'cat *', 'pwd', 'git status *', 'git diff *', 'npm run *',
   'curl *', ..., (Windows: 'dir *', 'tasklist *', 'systeminfo *')]
needsApproval:  // prompt the channel for yes/always/no
  ['npm publish *', 'git push *', 'docker *', 'curl * | sh',
   'pip install *', 'rm -r *', 'chmod *', 'mkdir *', 'powershell *', ...]
```

Pattern matching: `*` becomes `.*`, `?` becomes `.`, anchored.
Fallback: if the regex compile fails, prefix-match by stripping the
trailing ` *`.

CAQRS rule analogue (P3 Policy Gateway):

- `blocked` ≈ "never trade" set: e.g., `crypto perpetuals`, `0DTE
  options`, `leveraged ETFs > 2x`, anything not in the approved
  universe.
- `autoApproved` ≈ allowed without human review: cash equity in the
  approved universe, ETFs in the approved list, position size below
  N% of NAV.
- `needsApproval` ≈ requires human approval: anything outside the
  default risk envelope (size, sector concentration, options).

The same three-tier shape transfers cleanly. The patterns are
domain-specific (asset class + size + horizon instead of shell glob).

### 2. Inline yes/always/no UX

```ts
async requestScopeExternal(path, mode):
  if (!this.askHandler) return { allowed: false };
  const response = await this.askHandler(`Mercury needs ${mode} access to: ${path}`);
  if (response === 'always') { this.addScope(path, ...); return { allowed: true }; }
  if (response === 'yes')    { this.addTempScope(path, ...); return { allowed: true }; }
  return { allowed: false };
```

Three-valued response:

- `always`: persist to manifest
- `yes`: temporary scope (session only)
- (anything else): denied

The `askHandler` is set per-channel: CLI uses readline, Telegram uses
inline keyboards. The PermissionManager itself is channel-agnostic.

CAQRS Policy Gateway analogue: in research mode, "always" / "yes" /
"no" maps to "register this rule", "execute this single action", "do
not execute". Same channel-injection design.

### 3. Channel-aware approval flow (lines 287-364, esp. 336-360)

The auto-approve / blocked logic is identical on both CLI and
Telegram. The "approval-needed" path branches:

- On Telegram, `askHandler` is set, so the manager prompts inline.
- On CLI without an `askHandler` set yet, returns
  `{ allowed: false, needsApproval: true }` so the agent loop can
  defer to the read-line-based approval tool.

The same path is used for "no rule matched" — silent commands like
`make build` that are not in any list still need approval.

**CAQRS implication**: Policy Gateway should not assume an interactive
user. In headless mode, "no rule matched" → block. In CLI mode, it
prompts. In Slack-decision mode, it sends to `#trading-approvals`.

### 4. Skill elevation (lines 184-207)

```ts
elevateForSkill(allowedTools: string[]) {
  if (allowedTools.includes('run_command')) elevatedCommands.add('run_command');
  if (allowedTools.includes('read_file')) elevatedCommands.add('fs_read');
  if (allowedTools.includes('write_file')) elevatedCommands.add('fs_write');
}
```

When a skill is invoked with declared `allowed-tools`, the
PermissionManager temporarily widens permission for that skill's
duration. After the skill returns, `clearElevation()` resets.

CAQRS analogue: a research playbook can declare which data sources /
which broker actions it needs. The Policy Gateway elevates for the
duration of the playbook execution. Without this elevation, every
single tool call would re-prompt.

### 5. Path-traversal detection (lines 317-325, 448-466)

Before allowing a shell command, the manager checks if any argument
looks like a path outside CWD:

```ts
hasPathBeyondCwd(command: string): string | null {
  const pathPatterns = [
    /(?:^|\s)(\/[^\s]+)/,        // absolute /path
    /(?:^|\s)(~\/[^\s]+)/,        // home ~/path
    /(?:^|\s)\.\.\/([^\s]+)/,    // relative ../path
    /(?:^|\s)([A-Za-z]:\\[^\s]+)/,  // Windows C:\
    /(?:^|\s)(\\\\[^\s]+)/,      // UNC \\
  ];
  for (const p of pathPatterns) {
    const match = command.match(p);
    if (match) {
      const candidate = resolve(match[1].replace(/^~/, homedir()));
      if (!candidate.startsWith(this.cwd)) return candidate;
    }
  }
  return null;
}
```

If a path beyond CWD is detected, the filesystem scope check is
invoked for that path before the shell command runs. **A shell
command that tries to `rm` a path outside CWD must have explicit
filesystem write scope for the parent.**

CAQRS analogue: not directly relevant (no shell commands), but the
**defence-in-depth principle** applies: even within the Policy
Gateway, broker-side actions should be re-validated against the
declared portfolio universe even if the strategy claims compliance.

### 6. Temporary scopes (lines 423-437)

```ts
private tempScopes: FileScope[] = [];

addTempScope(path, read, write) { this.tempScopes.push({...}); }
private findTempScope(resolvedPath) { /* same matching as findScope */ }
```

Temporary scopes are session-only — never persisted, never serialised.
The agent has separate lookups for durable (`findScope`) and
temporary (`findTempScope`) scopes.

CAQRS analogue: per-cycle policy relaxations (e.g., "for this
backtest run, allow 50-symbol universe instead of 20-symbol default")
should be temporary unless explicitly committed to the manifest.

### 7. `setAutoApproveAll(true)` for internal tasks (lines 176-182)

For scheduled tasks and internal prompts, the manager bypasses all
approval prompts (the human is not present). The agent harness sets
this true at the start of an internal-prompt handle and false in the
finally clause.

CAQRS implication: cron-triggered cycles run with permissions wide
open inside the declared universe. Out-of-universe actions are still
blocked — the wide-open mode is "do not prompt, just enforce the
rules".

### 8. Pending-approval shortcut (lines 209-215, 305-309)

```ts
addPendingApproval(baseCommand: string) { this.pendingApprovals.add(baseCommand); }

// In checkShellCommand:
if (this.pendingApprovals.has(baseCmd)) { return { allowed: true }; }
```

When the user says "yes, run this command" on a one-off prompt, the
*base command* (first word) is added to a pending set so the next
invocation of the same command in this short session works without
re-prompting. After the agent emits a final response,
`clearPendingApprovals()` should be called (Mercury does this
implicitly via `finally` blocks elsewhere).

CAQRS analogue: when the operator approves a one-off "run backtest
for SPY today", the gateway can keep a *short-lived pending-approval*
set so a follow-up of the same scope (re-running the same backtest
with adjusted params) does not require a new approval round.

### 9. Manifest format example

```yaml
capabilities:
  filesystem:
    enabled: true
    scopes:
      - { path: ., read: true, write: true }
      - { path: ~/projects/myapp, read: true, write: true }
  shell:
    enabled: true
    blocked: [sudo *, rm -rf /, ...]
    autoApproved: [ls *, cat *, ...]
    needsApproval: [npm publish *, git push *, ...]
    cwdOnly: true
  git:
    enabled: true
    autoApproveRead: true
    approveWrite: true
```

YAML chosen for human-editability. CAQRS could use the same format
or jump to TOML (matches our `pyproject.toml`).

## What CAQRS already has

Nothing yet. P3 Policy Gateway is planned but not implemented.

## CAQRS implications

`caqrs/policy/` module structure:

```
caqrs/policy/
├── manifest.py         # pydantic schema for the policy manifest
├── gateway.py          # main entry; checks DecisionAction against manifest
├── rules/
│   ├── universe.py     # asset whitelist (analogue: filesystem scope)
│   ├── size.py         # position size limits (analogue: shell autoApproved)
│   ├── concentration.py # sector / single-name caps
│   ├── leverage.py     # margin / derivatives gate (analogue: shell blocked)
│   └── action_class.py # allowed action types (cash equity / ETF / options...)
├── elevation.py        # per-playbook scope widening
└── approval_handler.py # injectable yes/always/no handler (CLI / Slack / autorun)
```

The schema-level enforcement already in `StrategyDecision` (sum of
weights ≤ 1, no duplicate tickers, weight ≤ max_position_weight) is
the **first layer**. The Policy Gateway is the **second layer**: it
checks against the user's manifest of universe / size / concentration
/ allowed action classes.

The pattern matching engine is small enough to reimplement directly.
The yes/always/no UX with channel-injected `askHandler` transfers
cleanly.

## Open questions

- Mercury bundles git as a separate capability with `autoApproveRead`
  and `approveWrite` flags. CAQRS doesn't run git, but the **same
  per-capability flag pattern** ("read is auto-approved, write needs
  prompt") could apply to data sources: `read market data = auto`,
  `write trade order = approve`. The pattern is reusable beyond its
  literal git use.
- Mercury's `cwdOnly: true` for shell prevents commands from
  affecting paths outside the current working directory unless those
  paths have explicit scope. CAQRS analogue: a default `universeOnly:
  true` flag means trades only on assets in the declared universe;
  out-of-universe trades require explicit scope addition.
