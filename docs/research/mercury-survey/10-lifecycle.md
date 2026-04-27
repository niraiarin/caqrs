# 10 — Lifecycle State Machine (`src/core/lifecycle.ts`)

## Mercury source

- File: `src/core/lifecycle.ts`
- Lines: 44 (complete read — small file).

## Key types

```ts
type Transition = { from: AgentState; to: AgentState; condition?: () => boolean };

class Lifecycle {
  private state: AgentState = 'unborn';
  getState(): AgentState;
  transition(to: AgentState): boolean;  // returns false on invalid
  is(state: AgentState): boolean;
  canTransitionTo(to: AgentState): boolean;
}
```

## State transition table (lines 6-18)

```ts
const VALID_TRANSITIONS: Transition[] = [
  { from: 'unborn',     to: 'birthing'    },
  { from: 'birthing',   to: 'onboarding'  },
  { from: 'onboarding', to: 'idle'        },
  { from: 'idle',       to: 'thinking'    },
  { from: 'thinking',   to: 'responding'  },
  { from: 'responding', to: 'idle'        },
  { from: 'idle',       to: 'sleeping'    },
  { from: 'sleeping',   to: 'awakening'   },
  { from: 'awakening',  to: 'idle'        },
  { from: 'thinking',   to: 'idle'        },  // error path
  { from: 'idle',       to: 'onboarding'  },  // re-init path
];
```

State graph:

```
unborn → birthing → onboarding → idle ⇄ thinking ⇄ responding
                       ↑ ↑          │
                       └─┴───────── ┘ (re-init from idle)
                                   ↓
                                sleeping → awakening → idle
```

## Implementation patterns

### 1. Whitelist-only transitions

Every transition must be in the table. Invalid transitions log a
warning and return `false`. **The agent does not crash** on bad
transitions — the calling code is expected to check the return value
or `canTransitionTo` first.

CAQRS analogue: Pythonic equivalent could use:

```python
class OrchestratorState(StrEnum):
    IDLE = "idle"
    OBSERVING = "observing"
    HYPOTHESIZING = "hypothesizing"
    SCRUTINIZING = "scrutinizing"      # Skeptic phase
    RESEARCHING = "researching"
    AUDITING = "auditing"
    DECIDING = "deciding"
    REPORTING = "reporting"
    ERROR = "error"
```

With a frozen transition matrix as a `frozenset[tuple[State, State]]`.

### 2. State enum lives in `types/agent.ts`

Decoupled from the lifecycle implementation. CAQRS analogue: keep
`OrchestratorState` in `caqrs/orchestrator/state.py` separate from
the `Lifecycle` (or `OrchestratorStateMachine`) class.

### 3. Optional transition `condition` (line 4)

The `Transition` type allows a `condition: () => boolean` predicate,
but **none of the actual transitions use it**. The hook is reserved
for future use (e.g., "can only transition to sleeping if no pending
work").

CAQRS implication: include the same hook from day one. Conditions
will be needed for "can only transition to RESEARCHING if Hypothesis
has been adopted" or similar guards.

### 4. Logging on every transition (lines 30-33)

```ts
if (!valid) {
  logger.warn({ from: this.state, to }, 'Invalid state transition');
  return false;
}
logger.info({ from: this.state, to }, 'State transition');
```

Every state change is logged. CAQRS analogue: emit a
`StateTransition` event to the orchestrator's event log so post-hoc
audit (regret analysis, replay) is possible.

## What CAQRS already has

Nothing. Lifecycle hasn't been needed yet because the providers don't
have a state machine — they are stateless function-of-input.

## CAQRS implications

```python
# caqrs/orchestrator/state.py
from enum import StrEnum

class OrchestratorState(StrEnum):
    IDLE = "idle"
    OBSERVING = "observing"
    HYPOTHESIZING = "hypothesizing"
    SCRUTINIZING = "scrutinizing"
    RESEARCHING = "researching"
    AUDITING = "auditing"
    DECIDING = "deciding"
    REPORTING = "reporting"
    SLEEPING = "sleeping"     # idle long-running daemon
    ERROR = "error"

# caqrs/orchestrator/state_machine.py
from typing import Final

_VALID_TRANSITIONS: Final[frozenset[tuple[OrchestratorState, OrchestratorState]]] = frozenset({
    (OrchestratorState.IDLE, OrchestratorState.OBSERVING),
    (OrchestratorState.OBSERVING, OrchestratorState.HYPOTHESIZING),
    (OrchestratorState.HYPOTHESIZING, OrchestratorState.SCRUTINIZING),
    (OrchestratorState.SCRUTINIZING, OrchestratorState.RESEARCHING),
    (OrchestratorState.RESEARCHING, OrchestratorState.AUDITING),
    (OrchestratorState.AUDITING, OrchestratorState.DECIDING),
    (OrchestratorState.DECIDING, OrchestratorState.REPORTING),
    (OrchestratorState.REPORTING, OrchestratorState.IDLE),
    # error paths: any state → ERROR
    *((s, OrchestratorState.ERROR) for s in OrchestratorState),
    # recovery: ERROR → IDLE (manual recovery point)
    (OrchestratorState.ERROR, OrchestratorState.IDLE),
    # short-circuit paths
    (OrchestratorState.SCRUTINIZING, OrchestratorState.IDLE),  # Skeptic kills the hypothesis
    (OrchestratorState.AUDITING, OrchestratorState.IDLE),       # Auditor kills the strategy
})

class OrchestratorStateMachine:
    def __init__(self) -> None:
        self._state = OrchestratorState.IDLE
        self._listeners: list[Callable[[OrchestratorState, OrchestratorState], None]] = []

    @property
    def state(self) -> OrchestratorState: return self._state
    def is_in(self, state: OrchestratorState) -> bool: return self._state == state
    def can_transition_to(self, target: OrchestratorState) -> bool: ...
    def transition(self, target: OrchestratorState) -> bool: ...
```

This is small enough to write + test as a single P1.2 commit.

## Open questions

- Mercury's `unborn → birthing → onboarding` sequence reflects "first
  run setup" — soul files are seeded, identity asked. CAQRS does not
  have an onboarding flow (config is via `caqrs init` CLI, planned
  P1.4+). Skip the unborn / birthing / onboarding states; start
  directly at IDLE.
- The Mercury error path `thinking → idle` exists for cases where
  `handleMessage` throws and the lifecycle needs to recover. CAQRS
  needs a similar `(any_state) → ERROR → IDLE` recovery, but with
  explicit error logging into episodic memory.
