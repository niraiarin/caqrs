# Observability Spec — structured-log schema for the cycle event log

Status: formalized (Task #88).
Pairs with `docs/requirements/registry.yaml` rows `NFR-OBS-1`,
`NFR-OBS-2`, `NFR-OBS-3`, `NFR-OBS-4`, `NFR-OBS-5`.
Tests at `tests/test_correlation_id_propagation.py`.

This document is the canonical reference for the structured-event
contract emitted by `caqrs.orchestrator.event_log.EventLog`. Every
event is a frozen `CycleEvent` (see
`src/caqrs/orchestrator/events.py`); the log is append-only, optionally
JSONL-persisted, and replayable lossless via `EventLog.load_jsonl`.

The schema documented here is *frozen* from the point of view of an
auditor: changes that break any of the cross-event invariants below
require an ADR amendment. Adding a new `CycleEventKind` is allowed
without an ADR provided the new kind respects the universal envelope
fields and any per-kind required-payload contract documented for it.

## 1. Goals

The cycle event log serves three operational goals, in priority order.

1. **Audit trail**. For any cycle that landed an
   `ExecutionReport`, an auditor must be able to reconstruct the full
   chain of upstream artifacts and the policy/broker decisions that
   produced them, *without* reading source code or replaying the
   cycle. The chain must be expressible as a sequence of equalities
   over event-payload fields and artifact metadata fields (see §4).
2. **Debugging**. When a cycle aborts, the log must localise the
   failure: which agent failed, with which invocation id, at which
   state-machine transition, and after how many tokens. No event is
   silently dropped on abort — the cycle id is preserved on every
   event emitted before the abort point.
3. **Regression analysis**. The log shape is stable across runs so
   that off-line tooling (CycleStore replay, future telemetry sinks)
   can compare two cycles without schema-version branching.

Out of scope for this PR (deferred to a future task): metrics
backends, log shipping, structured-search indexing. See §5.

## 2. ID taxonomy

The schema uses six distinct id kinds. They are deliberately separate
axes; do not conflate them.

| ID kind | Lives on | Allocator | Purpose |
|---|---|---|---|
| `cycle_id` | `CycleEvent.cycle_id`, `CycleResult.cycle_id`, `CycleBudget.cycle_id` | `caqrs.orchestrator.events.new_cycle_id` (16-char hex, 64-bit entropy) | Stable across the entire cycle. Every event in a cycle log carries the same value; cross-cycle filtering uses this. |
| `event_id` | `CycleEvent.event_id` | `caqrs.orchestrator.events.new_event_id` (16-char hex, 64-bit entropy) | Unique per event. Used for de-duplication on JSONL replay and as a fan-out key for downstream listeners. |
| `run_id` (invocation) | `AGENT_INVOKED.payload.run_id`, `AGENT_SUCCEEDED.payload.run_id`, `AGENT_FAILED.payload.run_id` | Allocated by `CycleRunner._call_agent` via `caqrs.schemas.common.new_run_id` | Identifies a single invocation of an agent. Mirrored across the invoke/succeed (or invoke/fail) edge for one call. Allocated *before* the agent returns so failed calls still have a correlation key. |
| `run_id` (artifact) | `<Artifact>.metadata.run_id`, e.g. `StrategyDecision.metadata.run_id` | Allocated by the agent itself when it builds its `RunMetadata` | Identifies a *produced artifact*. Survives in `CycleStore`. The decision-trace chain uses this id, not the invocation id. |
| `decision_run_id` | `POLICY_GATEWAY_APPLIED.payload.decision_run_id`, `BROKER_EXECUTED.payload.decision_run_id` | Copied verbatim from `StrategyDecision.metadata.run_id` | Forms the artifact-id axis of the decision-trace chain (§4). Equal across both events and equal to the producing decision's metadata. |
| `source_decision_run_id` | `ExecutionReport.source_decision_run_id` | Copied verbatim from the `FeasibleAction.source_decision_run_id` (which itself is copied from the decision) | Closes the decision-trace chain on the artifact side. |
| `payload_hash` | `Provenance.payload_hash` (entities layer, out of orchestrator scope) | `hashlib.sha256(canonical_json(payload)).hexdigest()` | Out of the orchestrator-event-log scope; documented here only because auditors expect it adjacent to `run_id` in any payload-trace narrative. Enforced by `NFR-AUDIT-2`. |

The two `run_id` flavours (invocation vs artifact) are intentionally
different values for the same agent. The invocation id covers "this
call started at time T" and is available even if the agent crashes
before producing an artifact. The artifact id covers "this output
came out of that call" and is the id that downstream artifacts (gateway,
broker, execution report) cite. Tests assert each axis separately —
see `tests/test_correlation_id_propagation.py`
`test_agent_invoked_succeeded_run_ids_pair_per_invocation` for the
invocation axis and
`test_execution_report_source_decision_run_id_matches_decision` for
the artifact axis.

## 3. Per-event-kind required fields

Every `CycleEvent` carries the universal envelope:

* `event_id: str` — non-empty (Pydantic `min_length=1`), unique within
  the log;
* `cycle_id: str` — non-empty, identical to every other event in the
  same cycle;
* `kind: CycleEventKind`;
* `timestamp: datetime` — tz-aware, pinned to UTC by every constructor
  in `events.py`;
* `payload: dict[str, Any]` — kind-specific, see below.

Kind-specific required payload keys:

| Kind | Required payload keys | Source |
|---|---|---|
| `CYCLE_STARTED` | (none required; `observer_input_run_id` optional) | `cycle_started_event` |
| `CYCLE_COMPLETED` | `terminal_state`, `artifacts_emitted`, `total_token_in`, `total_token_out` | `cycle_completed_event` |
| `CYCLE_ABORTED` | `reason`, `at_state` | `cycle_aborted_event` |
| `AGENT_INVOKED` | `agent_name`, `run_id` (invocation) | `agent_invoked_event` |
| `AGENT_SUCCEEDED` | `agent_name`, `run_id` (invocation), `output_schema`, `token_in`, `token_out`, `latency_ms` | `agent_succeeded_event` |
| `AGENT_FAILED` | `agent_name`, `run_id` (invocation), `error` | `agent_failed_event` |
| `STATE_TRANSITION` | `src`, `dst` | `state_transition_event` |
| `LOOP_DETECTED` | `rule`, `tool`, `count`, `message` | `loop_detected_event` |
| `BUDGET_EXCEEDED` | `budget_kind`, `consumed`, `cap` | `budget_exceeded_event` |
| `POLICY_GATEWAY_APPLIED` | `decision_run_id`, `action`, `violation_count` | `policy_gateway_applied_event` |
| `BROKER_EXECUTED` | `decision_run_id`, `status`, `fill_count`, `reason` | `broker_executed_event` |

Each entry above is enforced by:

1. The typed constructors in `src/caqrs/orchestrator/events.py` — only
   path that should build events outside tests.
2. `tests/test_correlation_id_propagation.py::test_per_event_kind_required_payload_fields_present`
   — walks every event in a happy-path cycle log and asserts the
   required keys are present and non-empty for the subset of kinds
   that participate in the correlation chain.

## 4. Cross-event invariants — the decision-trace chain

For any non-aborted cycle that produced an `ExecutionReport`, the
following equalities hold simultaneously (all the same id):

```
StrategyDecision.metadata.run_id
  == POLICY_GATEWAY_APPLIED.payload.decision_run_id
  == BROKER_EXECUTED.payload.decision_run_id
  == FeasibleAction.source_decision_run_id
  == ExecutionReport.source_decision_run_id
```

This is the *artifact-id axis* of the decision-trace chain. It is the
chain an auditor walks: starting from a recorded `ExecutionReport`,
look up `source_decision_run_id`, find the matching
`POLICY_GATEWAY_APPLIED` and `BROKER_EXECUTED` events in the cycle log,
then locate the `StrategyDecision` artifact with that
`metadata.run_id` in `CycleStore`. The five equalities together
guarantee the auditor never has to guess which decision produced which
report.

A second, separate axis is the *invocation-id axis*: for each agent
invocation, `AGENT_INVOKED.payload.run_id ==
AGENT_SUCCEEDED.payload.run_id` (or `AGENT_FAILED.payload.run_id` on
the failure path). This ties every "we started a call" event to its
"we finished" event and is the basis of NFR-OBS-1's pairing invariant.

Universal cycle-scope invariants:

* every event in a single cycle's log shares the same `cycle_id`
  (asserted by
  `test_all_events_share_cycle_id`);
* every event in a single cycle's log has a unique `event_id`
  (asserted by `test_all_event_ids_are_unique`);
* every event timestamp is tz-aware UTC (asserted by
  `test_all_event_timestamps_are_tz_aware_utc`);
* an aborted cycle still satisfies the `cycle_id` invariant on every
  event emitted before the abort point (asserted by
  `test_aborted_cycle_still_carries_cycle_id_on_every_emitted_event`).

## 5. Future log-backend notes (out of scope for this PR)

The current event log is in-memory + optional JSONL. The schema
documented above is sufficient to drive any of the following without
reshaping the events themselves; they are tracked but not implemented.

* **Persistent search index**. JSONL tail-following into a
  duckdb/sqlite index keyed on `(cycle_id, kind, timestamp)` for
  multi-cycle aggregation. Allowed because the schema is stable.
* **Structured logger fan-out**. `EventLog.on_event` already supports
  a listener callback; a future structured-logger adapter (stderr in
  CI, OpenTelemetry in production) will register through that hook and
  is not allowed to mutate the event payload.
* **Telemetry sink for cost tracking**. NFR-COST-1 will roll up
  `(token_in, token_out)` from `AGENT_SUCCEEDED` and
  `CYCLE_COMPLETED` per provider per month. Sink shape is deferred
  until a production deployment target is chosen; the rollup query
  needs no schema changes.
* **Bidirectional traceability lint**. The `@pytest.mark.traces`
  marker (Task #80) currently only asserts forward references. A
  future tightening will assert that every formalized REQ-ID has at
  least one `@pytest.mark.traces` test physically attached.
