# ADR-0005: Policy Gateway as a stateless pure function

- **Status**: Accepted
- **Date**: 2026-04-28
- **Slices implemented**: P3.a (#50), P3.b (#52), P3.c (#51)

## Context

P3 introduces the **Policy Gateway** — the projection
`Π : StrategyDecision → FeasibleAction` that sits between the Decider's
agent emit point and any broker adapter. The gateway must enforce
account-level constraints the agent doesn't see (total deployed
notional, ticker allow/deny lists, today's accumulated loss against the
daily kill switch, future: lot sizes, position aggregation, sector
caps).

Two structural questions had to be settled before writing P3.a:

1. **Is the gateway a stateful object or a pure function?**
2. **When a decision violates a constraint, does the gateway "fix" it
   in place (drop the offending leg, scale notional down) or demote the
   whole decision to `defer`?**

## Decision

### 1. Pure function, not stateful object

`apply_policy_gateway(decision, config) -> FeasibleAction` is a
**pure function**. No I/O, no broker SDK, no in-memory account state.
The caller is responsible for assembling a fresh `PolicyGatewayConfig`
per invocation, computing whatever account state is needed (e.g. the
day's realized loss) from whatever ground-truth source is in use
(paper broker, live broker, simulator).

### 2. Demote-whole, never filter-partial

A decision with **any** violation is demoted to `DEFER` with
`targets=()` and the full violation list attached. The gateway never
silently drops offending targets and lets the rest through.

### 3. Closed enum of violation kinds, free-form context map

`PolicyViolationKind` is a closed `StrEnum`: every constraint class is
a named member. `PolicyViolation.context` is a free-form `str → str`
map so per-violation detail (offending ticker, signed remaining
budget, …) is JSON-serialisable without bespoke encoders.

## Decision drivers

### Pure function

- **Auditability.** Every projection is reproducible from the inputs
  alone. A cycle-log replay with the same `(decision, config)` pair
  always produces the same `FeasibleAction`. Stateful gateways turn
  audits into multi-step state reconstruction.
- **Testability.** Every constraint is a `_collect_violations`
  branch testable against a hand-built decision + config; no fixture,
  no mock, no clock. P3.a lands with 14 tests; P3.b adds 10 more.
  Integration concerns (where does the realized loss come from?) live
  outside the gateway and are tested separately at the caller.
- **Composition.** New constraints add one `PolicyViolationKind`
  member + one branch + (optionally) one config field. The function
  signature stays stable across P3.b, c, and the eventual P3.d / P4
  expansions.
- **Independence from broker choice.** A live broker, a paper broker,
  or an in-memory simulator can each plug into the same gateway by
  emitting `PolicyGatewayConfig` from their state. The gateway has no
  opinion about which.

### Demote-whole, never filter-partial

A decision with two longs and one short is a **portfolio**, not a bag
of independent orders. If the gateway drops the short on a deny-list
hit and keeps the longs, it has silently changed the decision's risk
profile from market-neutral to net-long. The agent didn't intend that
exposure; the operator didn't approve it. Either:

- the constraint is **wrong for this decision** and the agent should
  re-emit with the constraint relaxed (an operator-driven decision); or
- the constraint is **right** and the decision must be replaced
  wholesale with a corrected one.

Both routes are served by emitting `defer` + violations. Filter-partial
serves neither.

A future "explicit-permission-to-clip" mode can be added as an opt-in
flag if a caller actually needs partial execution (e.g. a sweeping
DCA strategy where one bad ticker shouldn't block the rest). The
default stays demote-whole.

### Closed enum + free-form context

A closed enum makes the catalogue of constraint classes a typed,
discoverable surface. Adding a new violation kind requires touching
the enum (and forces every consumer that pattern-matches on it to
update). A free-form `str → str` context map lets each kind carry
whatever detail is useful (ticker name for `TICKER_DENY_LISTED`,
remaining budget for `LOSS_BUDGET_EXHAUSTED`) without a per-kind
schema explosion. JSON-round-trip stays trivial.

## Consequences

### Accepted

- The caller owns account state. P3.b's `daily_realized_loss_usd`
  must be computed **somewhere** (paper broker, live broker,
  manual injection); the gateway is not that somewhere.
- Decisions that violate any rule are deferred whole, even when a
  partial execution would be technically feasible. Callers that want
  partial execution must either fix the source decision or build a
  separate "permissioned partial" pathway.

### Implications for P3.d (paper broker)

The paper broker (P3.d) tracks realized PnL and current positions in
its own state. The CycleRunner caller composes:

```
LossBudgetTracker(broker_state) → PolicyGatewayConfig.daily_realized_loss_usd
PositionAggregator(broker_state) → (future) PolicyGatewayConfig.held_positions
                                  → cycle runner → apply_policy_gateway
```

Each component is independently testable. The gateway stays pure even
as the surrounding loop accumulates state.

**Day-boundary semantics (binding for P3.d-3 LossBudgetTracker and any
future per-day projection):**

- **Trigger**: the **CycleRunner caller** owns the day-boundary
  signal. The canonical pattern is calling
  `tracker.mark_start_of_day(today, broker)` at cycle 0 of each
  trading day. Neither the gateway, the tracker, nor the broker is
  allowed to discover "today" on its own (no `datetime.now()` inside
  any of them).
- **Timezone**: `today` is a tz-aware `date` derived from the same
  timezone the active `ResearchPlan` / `WalkForwardWindow` uses (UTC
  by default; venues with a non-UTC trading calendar pass their own
  `today` value).
- **Tracker statelessness**: the same "no I/O / no clock / no
  internal state beyond what was injected" rule that justifies a pure
  gateway applies **recursively** to its caller-side projections. A
  `LossBudgetTracker` may hold a single day-boundary baseline (a
  `Decimal`) but does not retain references to broker state, does not
  poll, and does not call `apply_policy_gateway` itself. It computes
  `magnitude = max(0, baseline - source.realized_pnl_usd)` and
  returns; the runner injects the result into a fresh
  `PolicyGatewayConfig`.

**Scope of "in-memory state" in Decision 1**: the prohibition applies
to the gateway projection itself, not to the surrounding pipeline.
Brokers, trackers, and aggregators are explicitly allowed to hold
state — the rule is that `apply_policy_gateway` consumes their
*output* via a per-call `PolicyGatewayConfig`, not their handles.

### Implications for P4 (live broker)

P4 substitutes the live broker's account API for the paper broker's
in-memory state. The gateway projection is identical; only the source
of `PolicyGatewayConfig.daily_realized_loss_usd` changes. This keeps
the safety semantics testable end-to-end against the paper environment
before any real funds are at risk.
