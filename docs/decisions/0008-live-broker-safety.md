# ADR-0008: Live-broker safety NFR perimeter (P4 prerequisite)

- **Status**: Accepted
- **Date**: 2026-05-02
- **References**: Codex GPT-5.5 audit of the CAQRS pipeline (April–May 2026,
  Security 30% / Reliability 55%); ADR-0005 (Policy Gateway as a stateless
  pure function); ADR-0006 (two-step TDD dispatch); ADR-0007 (verifier
  reports as committed audit artifacts)

## Context

The CAQRS roadmap's **P4 phase** introduces the first **live broker
adapter** (Interactive Brokers, Alpaca, Tachibana, …). Until P4 the only
broker in the codebase is `PaperBroker`
(`src/caqrs/execution/paper_broker.py`), a long-only rebalance simulator
that holds positions and realized PnL in process memory and never sends
real orders.

A live broker introduces three risk classes the paper environment does
not have:

1. **Real money loss** — every order can move the account; a buggy
   rebalance can drain capital faster than a human can intervene.
2. **Real authn / authz risk** — broker credentials buy execution
   authority, not just data; a leaked key is structurally different from
   a leaked j-Quants token (cf. NFR-SEC-1, NFR-SEC-2).
3. **Real audit / compliance risk** — order timestamps, fill prices, and
   cancellation behaviour become evidence in any post-trade
   reconstruction or regulatory inquiry (cf. NFR-AUDIT-1, NFR-AUDIT-2).

ADR-0005 already established the **portfolio-level safety perimeter**
through the pure-function policy gateway: a decision that violates any
constraint is demoted whole to `DEFER`, and `daily_realized_loss_usd`
gates the kill-switch path. That perimeter is necessary but not
sufficient for live trading. A live broker can fail in ways the gateway
cannot anticipate from the decision side alone — broker-side rejects,
network partitions during order submission, partial fills, venue-side
cancellations, idempotent-replay ambiguity. These are **broker-layer**
concerns, not gateway-layer concerns.

The Codex audit's verdict on this is direct: P4 should not begin until
the live-broker safety NFRs are formalized. The audit graded
**Security** at 30% and **Reliability** at 55% on the strength of "live
trading is on the roadmap and there are no committed boundaries that
bind it." This ADR fills that gap **before** any P4 implementation,
**before** the contract test suite (Task #87), and **before** any
LiveBroker class lands.

## Decision

Any future `LiveBroker` implementation MUST satisfy seven non-functional
requirements, each registered in `docs/requirements/registry.yaml` as
`NFR-LIVE-BROKER-N` with `kind: non-functional`,
`subsystem: execution`, and `status: deferred` until the LiveBroker
implementation lands. The contract test suite in Task #87 will fill
each NFR's `tests:` list with assertions against these requirements.

### NFR-LIVE-BROKER-1: Default-off

The LiveBroker implementation MUST be opt-in via a config flag
(canonical name: `enable_live_orders: bool = False`). Calling
`LiveBroker.execute()` while the flag is `False` MUST return
`ExecutionStatus.SKIPPED` with reason `"live orders disabled"` — never a
silent no-op, never a paper passthrough. The flag MUST require an
**explicit human approval workflow** to flip: an env var that requires
manual setting plus a one-time CLI confirmation (the exact wiring is a
P4-PR concern, but the two-step gate is binding).

### NFR-LIVE-BROKER-2: Credential isolation

Live broker credentials MUST live under the env var prefix
`LIVE_BROKER_*` (e.g. `LIVE_BROKER_API_KEY`,
`LIVE_BROKER_API_SECRET`), distinct from existing data-source
credentials (`JQUANTS_API_KEY`, `EDINET_API_KEY`, …). The PaperBroker
MUST NOT import or read any `LIVE_BROKER_*` env var; the converse
(LiveBroker reading j-Quants creds) is also forbidden. Compliance is
verified by a **static import audit** — either a new
`scripts/check_credential_isolation.py` or an extension of
`scripts/check_data_source_tos.py` (gated by Task #88 lint or a
dedicated lint). All credentials MUST be encrypted at rest via dotenvx,
which is the established convention for this repo.

### NFR-LIVE-BROKER-3: Dry-run parity

Every live order MUST first be simulated by
`PaperBroker.execute(action, prices)` and the result asserted to be
`ExecutionStatus.FILLED`. If the paper-broker simulation rejects (any
non-`FILLED` status, including `SKIPPED`, `INSUFFICIENT_FUNDS`, etc.),
the live broker MUST also reject and emit
`BROKER_LIVE_REJECTED` (see NFR-LIVE-BROKER-7) without sending the
order. This guarantees **no live order is sent that the paper broker
wouldn't have filled** — the paper environment becomes a mandatory
pre-flight check, not just a backtest fixture.

### NFR-LIVE-BROKER-4: Idempotency key on every order

Every order sent to a live venue MUST carry a deterministic idempotency
key. Replaying the same `(FeasibleAction, prices, cycle_id)` MUST
produce identical broker behaviour: same orders, same idempotency key,
same fill semantics on the venue's side. The key SHOULD be derived as
`sha256_hex((cycle_id, decision_run_id, ticker, side, quantity))`;
including `decision_run_id` (unique per cycle) prevents collisions
across re-runs of the same `cycle_id`. Venues that do not natively
support an idempotency token MUST log the computed key alongside the
venue-assigned order id so that replay-vs-fresh-order disambiguation is
recoverable post-hoc.

### NFR-LIVE-BROKER-5: Kill-switch

A `LiveBroker.kill_switch()` method MUST abort any in-flight orders
within **1 cycle** (canonical bound: ≤ 1 CycleRunner iteration after
invocation). The kill switch MUST be invocable from outside the cycle
loop — at minimum via an API endpoint, with a signal handler (SIGUSR1
or equivalent) as a defence-in-depth path. Once invoked, the live
broker MUST refuse new orders (return
`ExecutionStatus.SKIPPED` with reason `"kill switch engaged"`) until
explicitly **re-enabled by a human** through the same approval
workflow that flips NFR-LIVE-BROKER-1's `enable_live_orders` flag.

### NFR-LIVE-BROKER-6: Daily loss cap (defense in depth)

`PolicyGatewayConfig.daily_loss_limit_usd` already enforces a
**portfolio-level** cap on the gateway side (per ADR-0005). The
LiveBroker MUST ALSO enforce its **own** daily realized-PnL cap, set
independently from the gateway and configured per the live-broker
config (canonical name: `live_broker_daily_loss_cap_usd`). If realized
loss exceeds the broker-level cap, the live broker MUST automatically
engage `kill_switch()` (NFR-LIVE-BROKER-5) and emit
`BROKER_LIVE_KILL_SWITCH`. This is **intentional double-checking**: the
gateway and the broker MUST NOT share state for this check — duplicate
calculation is the safety property, not an inefficiency to be
optimised away.

### NFR-LIVE-BROKER-7: Distinct event taxonomy

The cycle event log MUST distinguish live-broker activity from
paper-broker activity at the type level. New `CycleEventKind` members
for live broker:

- `BROKER_LIVE_SUBMITTED` — order accepted by the venue
- `BROKER_LIVE_FILLED` — order filled (full or partial; partial fills
  emit one event per fill)
- `BROKER_LIVE_REJECTED` — venue rejected the order, or
  NFR-LIVE-BROKER-3 dry-run parity rejected before submission
- `BROKER_LIVE_CANCELLED` — order cancelled (venue-initiated or
  kill-switch-initiated)
- `BROKER_LIVE_KILL_SWITCH` — kill switch engaged (manual or auto via
  NFR-LIVE-BROKER-6)

Live-broker events MUST NOT use `BROKER_EXECUTED`, which stays
paper-only. This keeps the cycle log readable for audit: a `grep
BROKER_LIVE_` against an EventLog answers "did this cycle touch real
money?" without any further analysis.

## Consequences

### Positive

- **Concrete acceptance criteria for P4.** A future LiveBroker PR has
  seven hard checkboxes, not a vague safety mandate. Task #87's
  contract test suite has named NFRs to assert against.
- **Defense in depth.** NFR-LIVE-BROKER-3 (paper parity) +
  NFR-LIVE-BROKER-6 (broker-level loss cap) + NFR-LIVE-BROKER-1
  (default-off) reduce the real-money-loss blast radius to the **weakest
  link** rather than the broker layer alone. A bug in any single layer
  is contained by the others.
- **Audit-grade event taxonomy.** NFR-LIVE-BROKER-7 makes "did this
  cycle touch real money?" a one-line grep against the EventLog,
  which directly addresses the Codex audit's reliability finding.

### Negative

- **P4 PRs will be larger.** The dry-run-parity check
  (NFR-LIVE-BROKER-3) and the duplicate broker-side loss cap
  (NFR-LIVE-BROKER-6) add code that is not strictly necessary for live
  execution. This is acceptable: the cost is one-time, the benefit is
  permanent.
- **No partial-execution path on the live side.** Consistent with
  ADR-0005's demote-whole rule on the gateway side. A future
  "permissioned partial" pathway is explicitly out of scope here.

### Risks (and mitigations)

| Risk | Mitigation |
| --- | --- |
| Idempotency-key collision across re-runs of the same `cycle_id` | NFR-LIVE-BROKER-4 mandates `decision_run_id` (unique per cycle) in the key derivation. |
| Kill-switch implementation depends on the venue's API; some venues do not support cancellation | A per-venue ADR (ADR-0009 IB / ADR-0010 Alpaca / …) will document venue-specific gaps. Where cancellation is not supported, the kill switch MUST at minimum stop submitting new orders and surface a `BROKER_LIVE_KILL_SWITCH` event with a "venue-cancellation-unsupported" reason. |
| Live credentials leak into the paper code path (or vice versa) | NFR-LIVE-BROKER-2 static import audit; lint enforced via Task #88 or a dedicated rule. |
| Operator forgets to flip `enable_live_orders` and assumes orders are firing | NFR-LIVE-BROKER-1's `SKIPPED` return + reason string is observable in the EventLog as `BROKER_LIVE_REJECTED` (per NFR-LIVE-BROKER-7); no silent no-op. |
| `daily_loss_limit_usd` and `live_broker_daily_loss_cap_usd` drift apart | Intentional. Drift is the property; if both layers miscompute the same way, the duplicate check is worthless. Per-cycle CycleRunner test (#87) asserts the broker cap is read independently of the gateway config. |

## Implementation checklist (for the future P4 PR)

- [ ] `LiveBroker` class implementing `BrokerProtocol` and the seven
      NFRs above
- [ ] Static import lint in `scripts/check_credential_isolation.py`
      (or extension of `scripts/check_data_source_tos.py`) for
      NFR-LIVE-BROKER-2
- [ ] Per-venue ADR (ADR-0009 IB / ADR-0010 Alpaca / …) capturing
      venue-specific gaps in kill-switch, idempotency, and partial-fill
      semantics
- [ ] Two-step TDD dispatch per ADR-0006 (failing tests + types in
      Step 1, implementation flips xfail to pass in Step 2)
- [ ] Verifier artifact per ADR-0007 (live-broker PRs are always
      "high-risk" per ADR-0007's trigger 6 and additionally subject to
      cross-family + human-gate escalation per agent-manifesto ADR-027)
- [ ] Human review required before merge — non-negotiable, regardless
      of how clean the verifier report is

## Out of scope

- **The LiveBroker class itself.** This ADR establishes the perimeter;
  the implementation lands in P4.
- **The contract test suite.** Task #87 is the dedicated dispatch.
- **Per-venue ADRs.** Deferred to P4; the placeholders ADR-0009 /
  ADR-0010 are forward pointers, not commitments to a specific venue
  set.
- **Modification of `docs/requirements/non-functional.md`.** A
  follow-up task before P4 will extend the NFR catalogue with the
  measurable thresholds; this ADR adds the IDs to
  `docs/requirements/registry.yaml` only.

## Reconsider when

- A live venue is selected and its API surface forces a deviation from
  any of the seven NFRs (most likely NFR-LIVE-BROKER-5 kill-switch
  semantics on a venue without native cancellation).
- A second live broker lands (multi-venue mode); some NFRs may need to
  be parameterised per venue rather than enforced globally.
- The Codex audit re-runs and Security / Reliability scores improve;
  the seven NFRs may be tightened further if the bar is met early.
