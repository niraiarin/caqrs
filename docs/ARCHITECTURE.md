# Architecture

## Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│ Heartbeat / external trigger                                            │
└──────────┬──────────────────────────────────────────────────────────────┘
           │  ObserverInput (universe + horizon + optional polymarket signals)
           ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ CycleQueue (FIFO, serial dispatch, reentrancy guard)                    │
└──────────┬──────────────────────────────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ CycleRunner — typed pipeline driver                                     │
│                                                                         │
│  Observer ─► Hypothesis ─► Skeptic ─► Research ─► [Backtest] ─►        │
│              Auditor ─► Decider                                         │
│                                                                         │
│  side rails: OrchestratorStateMachine | EventLog | BudgetGuard          │
└──────────┬───────────────────────────────────────────────────┬──────────┘
           │ CycleResult + per-cycle events                     │ data
           ▼                                                    │ (Observer)
┌──────────────────────────┐                              ┌─────▼────────┐
│ CycleStore               │                              │ caqrs.data.  │
│ cycles/<id>/result.json  │                              │ polymarket   │
│ cycles/<id>/events.jsonl │                              │ (CLOB+Gamma) │
│ index.jsonl              │                              └──────────────┘
└────────────┬─────────────┘
             │ StrategyDecision
             ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Policy Gateway Π : StrategyDecision → FeasibleAction (P3.a + b)         │
│  - notional cap + ticker allow/deny lists                               │
│  - daily loss-budget consumption (caller-supplied)                      │
│  Demote-whole on any violation; never filter-partial.                   │
└──────────┬──────────────────────────────────────────────────────────────┘
           │ FeasibleAction
           ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Paper Broker (P3.d, pending) → Live Broker (P4, gated by human)         │
└─────────────────────────────────────────────────────────────────────────┘
```

## Orchestrator state machine

States represent research-cycle phases (not agent lifecycle). Whitelist transitions
enforced at runtime in `caqrs.orchestrator.state_machine`:

```
idle → observing → hypothesizing → scrutinizing →┐
                                                 ├─► researching → auditing →┐
                                                 │                            ├─► deciding → idle
                                                 │   (verdict=PASS only)      │
                                                 │                            └─► idle (verdict=FAIL)
                                                 └─► idle (skeptic non-PROCEED)
                                       (skeptic kill / require_revision)

* → error → idle    (any agent failure / budget breach)
```

Listener fan-out emits a `STATE_TRANSITION` event into the `EventLog` on every
successful transition.

## Agent contract

All agents implement `caqrs.agents.protocol.Agent[I, O]`. `I` and `O` are pydantic
`BaseModel` subclasses, frozen and `extra="forbid"`. Concrete agents (Observer,
Hypothesis, Skeptic, Research, Auditor, Decider) inherit from `LLMAgent[I, O]`
and receive typed inputs only — they do not see other agents' raw outputs except
through `RunMetadata.parent_id` chains.

Agents are async pure functions of their input plus side-channel resources (LLM
provider, data store handles). Mutable shared state is forbidden inside agent
bodies; lineage is the only cross-agent communication channel.

## Artifact lineage

Every artifact carries `RunMetadata` with:

- `run_id`: stable 16-char hex identifier (64 bits of entropy).
- `parent_id`: `run_id` of the predecessor artifact (None for roots).
- `agent_name`, `model_id`, `created_at` (tz-aware), `llm_cost_usd`,
  `latency_ms`, `token_in`, `token_out`.

This makes regret analysis, ablation, and cost reports a query, not an
instrumentation exercise.

## Walk-forward as a schema-level invariant

`ResearchPlan.walk_forward` enforces train/test ordering and disjointness at the
schema validator level. In-sample-only configurations cannot be constructed
because every walk-forward window requires
`train_start < train_end <= test_start < test_end`. Cross-window test-set
overlap is also rejected. This pushes leakage prevention from "remember to test"
into "cannot compile".

## Cycle runtime: events, budget, persistence

- **`EventLog`** is an append-only stream of `CycleEvent` records. Listener
  callbacks fan out on every append; an optional `persist_to: Path` arg writes
  JSONL to disk in lock-step.
- **`BudgetGuard`** wraps a `CycleBudget` (token + wallclock cap). Callers feed
  agent token usage in via `consume(token_in=, token_out=)`; the guard emits a
  `BUDGET_EXCEEDED` event into the log on the *first* breach (further breaches
  stay silent) and the runner aborts the cycle.
- **`CycleStore`** (in `caqrs.memory`) persists each cycle's `CycleResult` plus
  its events to disk under `cycles/<id>/`, with a one-line summary appended to
  a rolling `index.jsonl`. `prune_older_than(cutoff)` keeps the archive bounded.
- **`CycleRunner`** integrates the above with the six agents, handling state
  transitions, agent invocation, error / budget propagation, and (optionally)
  auto-persist via the structural `CycleStoreProtocol`.
- **`CycleQueue`** is a serial dispatcher with a reentrancy guard so multiple
  callers can enqueue safely while cycles run one at a time.
- **`Heartbeat`** is a pure interval-based fire tracker: caller polls
  `is_due()` and calls `fire()` after enqueuing. Composes with `CycleQueue` in
  the caller's event loop.

## External data (Observer)

`caqrs.data` hosts read-only adapters that the Observer composes from. Each
sub-package wraps a single source:

- `caqrs.data.polymarket` — implied probabilities from prediction markets via
  the public CLOB + Gamma APIs (no auth). The helper
  `fetch_polymarket_signal(gamma_client=, clob_client=, identifier=)` resolves
  a market by id or slug, fetches per-token midpoint / spread / last-trade,
  and returns a `PolymarketSignal` ready to drop onto an `ObserverInput`.
- `caqrs.data.jquants` — JPX-official daily OHLCV + listed-issue master
  (free tier, env-var auth). `fetch_jquants_asset_snapshot(client=, code=, as_of=None)`
  produces an Observer-facing snapshot.

Future data sources (price feeds, news, macro) will follow the same shape:
async client per source, typed artifact, helper that composes raw responses
into the Observer-facing snapshot. CAQRS does not bundle a single opinionated
data layer because financial-research signals come from many uncorrelated APIs.

## Backtest layer (P2)

`caqrs.backtest` is a two-tier composition over the walk-forward schema:

- **Engine** — `run_walk_forward(plan, prices, signals, notional_usd)` is a pure
  function over polars DataFrames. Per fold it pivots prices to wide form,
  derives daily returns via `pct_change`, applies a one-day signal lag (so
  day-1 signals become day-2 positions — no lookahead bias), shifts costs
  forward, and returns a `BacktestReport` with per-fold + aggregate metrics.
- **Strategy templates** — `caqrs.backtest.templates` exposes a discriminated
  union over the built-in templates (`BuyAndHoldSpec`, `MomentumSpec(lookback_days, top_k)`,
  `MeanReversionSpec(lookback_days, bottom_k)`) plus a single `make_jquants_executor`
  dispatch entry point that returns a `BacktestExecutor` ready to plug into
  `CycleRunner`. The momentum / mean-reversion variants share a `_rank_signals`
  helper that selects winners or losers symmetrically; missing-data tickers
  are sentineled so they never enter the selection. Adding a template means
  adding one Spec class + one branch in the dispatcher.

The split between engine and templates keeps the engine source-agnostic (any
`PriceProvider` + signal DataFrame works) while the J-Quants factories pre-fetch
a `2 × lookback_days` calendar buffer before the earliest test start so day-1
signals already have a computable lookback return.

## Policy Gateway (P3)

The gateway is **not** a wrapper over a broker SDK. It is a pure-function
projection `Π : StrategyDecision → FeasibleAction` applied between the
Decider and any broker adapter. Decisions that violate the projection are
**never partially executed**: the gateway demotes the whole decision to
`defer` (with `targets=()`) and attaches the full violation list, because
silently dropping offending legs would distort the agent's intended risk
profile (the rejected leg may be the hedge). Callers — agent or supervisor
— re-emit a corrected decision instead of receiving a quietly-clipped one.

The `StrategyDecision` schema already enforces baseline cash-only
constraints (sum of weights ≤ 1, per-position weight ≤
`max_position_weight`, no duplicate tickers) at construction time. The
gateway adds the **account-level** layer the agent doesn't see:

- **P3.a — notional cap + ticker allow/deny lists** (`PolicyGatewayConfig.account_notional_cap_usd`, `allowed_tickers`, `denied_tickers`).
- **P3.b — daily loss-budget consumption** (`daily_realized_loss_usd`, demote when remaining ≤ 0).
- **P3.c — wired into `CycleRunner`** via the optional `policy_gateway_config` ctor arg; emits a `POLICY_GATEWAY_APPLIED` event with violation count and final action.

The gateway never **computes** account state (current realized loss,
current positions, lot sizes). Callers — eventually a `LossBudgetTracker`
reading paper-broker state, or a live broker's PnL feed — assemble a
fresh `PolicyGatewayConfig` per cycle. This keeps the projection pure,
deterministic, and trivially testable; new constraint classes (lot
rounding, position aggregation, sector caps) extend the config + add a
violation kind without touching the function signature.

P3.d (pending) layers a paper broker behind the gateway so the full
research → backtest → decide → project → execute loop runs end-to-end on
recorded fills, without touching real funds.

## Graceful degradation

Every external dependency (LLM provider, data source, broker) returns through
a provider abstraction with ordered fallback. A failed provider does not
crash the orchestrator; it produces an `AgentResult` with `error` set, which
the runner routes to `CYCLE_ABORTED` and the cycle's event log without
halting the surrounding loop. A future cycle with a healthy provider succeeds
without operator intervention.

The Polymarket helper specifically degrades per-outcome: if `get_midpoint`
fails for one token the snapshot still records the token's other available
fields rather than aborting the whole signal.
