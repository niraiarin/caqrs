# Architecture

## Overview

```
┌────────────────────────────────────────────────────────────┐
│ Orchestrator (state machine, asyncio)                      │
└──────┬─────────────────────────────────────────────────────┘
       │ pydantic-typed messages
       ▼
┌─────────────┬─────────────┬─────────────┬─────────────┐
│ Observer    │ Hypothesis  │ Skeptic     │ Research    │
│ (data)      │ (LLM)       │ (LLM)       │ (backtest)  │
└─────────────┴─────────────┴─────────────┴─────────────┘
       │                                         │
       ▼                                         ▼
┌────────────────────┐               ┌────────────────────┐
│ Memory             │               │ Artifact Store     │
│ (SQLite + FTS5)    │               │ (Parquet + JSON)   │
└────────────────────┘               └─────────┬──────────┘
                                                │
                                                ▼
                                     ┌─────────────────────┐
                                     │ Policy Gateway (Π)  │
                                     │ (P3)                │
                                     └─────────────────────┘
                                                │
                                                ▼
                                     ┌─────────────────────┐
                                     │ Paper / Live Broker │
                                     │ (P3 / P4)           │
                                     └─────────────────────┘
```

## State machine (orchestrator)

```
unborn → bootstrapping → idle ⇄ running → reporting → idle
                                  ↓
                                error → idle (with episode logged)
```

Implementation lands in P1.

## Agent contract

All agents implement `caqrs.agents.protocol.Agent[I, O]`. `I` and `O` are
pydantic `BaseModel` subclasses. Concrete agents register with the orchestrator
at startup and receive typed inputs only — they do not see other agents'
raw outputs except through `RunMetadata.parent_id` chains.

Agents are async pure functions of their input plus side-channel resources
(LLM client, data store handles). Mutable shared state is forbidden inside
agent bodies; lineage is the only cross-agent communication channel.

## Artifact lineage

Every artifact carries `RunMetadata` with:

- `run_id`: stable 16-char hex identifier (64 bits of entropy).
- `parent_id`: `run_id` of the predecessor artifact (None for roots).
- `agent_name`, `model_id`, `created_at` (tz-aware), `llm_cost_usd`,
  `latency_ms`, `token_in`, `token_out`.

This makes regret analysis, ablation, and cost reports a query, not an
instrumentation exercise.

## Walk-forward as a schema-level invariant

`ResearchPlan.walk_forward` enforces train/test ordering and disjointness at
the schema validator level. In-sample-only configurations cannot be
constructed because every walk-forward window requires
`train_start < train_end <= test_start < test_end`. Cross-window test-set
overlap is also rejected. This pushes leakage prevention from "remember to
test" into "cannot compile".

## Policy Gateway position (P3)

The gateway is **not** a wrapper over a broker SDK. It is a projection
`Π : Action → FeasibleAction` applied to `StrategyDecision` artifacts before
any broker adapter is reachable. Decisions that violate the projection are
not "fixed" in place; they are emitted with `action=defer` and a violation
report attached. Schema enforces baseline cash-only constraints (sum of
weights ≤ 1, per-position weight ≤ `max_position_weight`, no duplicate
tickers) without yet binding to a broker.

## Graceful degradation

Every external dependency (LLM provider, data source, broker) returns through
a provider abstraction with ordered fallback. A failed provider does not
crash the orchestrator; it produces an `AgentResult` with `error` set, which
the orchestrator routes to the episode log without halting the loop.
