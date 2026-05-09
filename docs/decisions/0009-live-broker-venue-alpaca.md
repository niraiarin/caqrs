# ADR-0009: Alpaca as the first live-broker venue (P4 proving ground)

- **Status**: Accepted
- **Date**: 2026-05-09
- **References**: ADR-0008 (live-broker safety NFR perimeter); ADR-0005
  (Policy Gateway as a stateless pure function); ADR-0006 (two-step TDD
  dispatch); ADR-0007 (verifier-report artifact); cross-family review
  by Codex GPT-5.5 on the broader CAQRS roadmap (2026-05-03 dispatch)
- **Implementation context**: Task #88 credential-isolation lint
  (PR #89) lands the static-graph gate ADR-0008 §NFR-LIVE-BROKER-2
  cited; Task #87 contract test suite (PR #87) holds the four xfailed
  assertions LiveBroker must flip. ADR-0009 selects the venue against
  which those assertions will first turn green.

## Context

ADR-0008 fixed the **safety perimeter** any `LiveBroker` must satisfy
(seven NFRs, default-off through distinct event taxonomy). It deferred
two questions to per-venue ADRs (the placeholder pointers ADR-0009 /
ADR-0010 in §"Implementation checklist"):

1. **Which venue lands first?** The contract test suite expects a
   concrete adapter implementation to flip its four LiveBroker-only
   xfail markers. Picking the wrong venue first burns weeks on
   integration plumbing while delaying the safety-contract proof.
2. **Where do venue-specific gaps in NFR-LIVE-BROKER-1..7 land?**
   Each venue has API quirks: kill-switch primitives differ, partial
   fills emit different webhook semantics, idempotency tokens have
   different length limits. ADR-0008's risk table calls these out
   explicitly and routes them to the per-venue ADR.

Codex GPT-5.5 cross-family review of the CAQRS roadmap (2026-05-03)
recommended **Alpaca** as the first venue, despite the J-Quants
(Japan-equity) bias of the upstream research stack. The reasoning was
direct: ADR-0008 is primarily a *safety architecture exercise*, not a
*market coverage exercise*. Picking the venue with the cleanest
disposable proving ground (paper trading, well-documented REST,
client-side idempotency tokens, one-call cancel-all) compresses the
P4 timeline; picking Japan-native infrastructure first shifts the
critical path onto venue-specific quirks that have no relation to the
safety contract.

The candidates considered:

| Venue | Pros | Cons |
| --- | --- | --- |
| **Alpaca** | Paper-trading endpoint at `paper-api.alpaca.markets`; REST + websocket; native `client_order_id` for idempotency; cancel-by-ID + cancel-all + `trade_suspended_by_user` toggle for kill-switch; first-party Python SDK (`alpaca-py`) matching CAQRS's `httpx`+SDK pattern | US-only equities + options; no J-Quants market overlap |
| Interactive Brokers | Global coverage incl. Japan; strong cancellation primitives (per-order + global cancel) | Heavy operational model (TWS/IB Gateway local session); WebAPI alternative still adds connection-state complexity; first P4 PR would be dominated by session plumbing |
| Tachibana e-Shiten | Japan-native; matches J-Quants research output 1:1 | Public docs sparse; quirks visible only after engagement; no English-language SDK; introduces venue-specific failure modes that compete for attention with the safety contract |

The Codex review's verdict ("Alpaca first … internal LiveBroker
contract proven before market-coverage expansion") matched the local
analysis. Tachibana stays on the roadmap as **ADR-0010** but does not
gate P4 first-cut.

## Decision

The first concrete `BrokerProtocol` implementation for live trading
(P4 first-cut) is **Alpaca paper trading**, integrated via the
first-party `alpaca-py` SDK against `https://paper-api.alpaca.markets`.

The implementation MUST:

- Live under `caqrs.execution.live_broker_alpaca` (new module). The
  generic boundary `caqrs.execution.live_broker` declared in
  `caqrs.lint.credential_isolation.DEFAULT_BOUNDARIES` will be
  promoted to `caqrs.execution.live_broker_alpaca` when the
  implementation lands; in the meantime the credential-isolation
  lint skips silently per its missing-boundary contract.
- Read credentials exclusively from `LIVE_BROKER_*` env vars
  (canonical names: `LIVE_BROKER_API_KEY`, `LIVE_BROKER_API_SECRET`,
  `LIVE_BROKER_BASE_URL`). The Alpaca-native env-var names
  (`APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`) MUST NOT be read
  directly by CAQRS code; the adapter rebrands them at the
  boundary.
- Default `enable_live_orders=False`; flipping the flag requires
  the two-step human approval workflow specified in
  ADR-0008 §NFR-LIVE-BROKER-1.
- Carry the `BrokerProtocol.execute(*, action, prices)` shape
  unchanged. Alpaca's order-submission API receives the per-target
  fills derived from `FeasibleAction` exactly as the PaperBroker
  does today.
- Run every order through `PaperBroker.execute(action, prices)` as
  a pre-flight check (NFR-LIVE-BROKER-3) before any Alpaca API
  call.

### Per-NFR mapping (how Alpaca satisfies ADR-0008)

| NFR | Alpaca-side mechanism |
| --- | --- |
| NFR-LIVE-BROKER-1 (default-off) | `LiveBrokerAlpaca(enable_live_orders=False, ...)` returns `ExecutionStatus.SKIPPED` on `execute()`. Flipping the flag requires `LIVE_BROKER_ENABLE_LIVE_ORDERS=1` plus a one-time CLI confirm via `python -m caqrs.execution.live_broker_alpaca confirm-live` (the exact CLI shape lands in P4). |
| NFR-LIVE-BROKER-2 (credential isolation) | `LIVE_BROKER_*` env-var prefix only; no `JQUANTS_*` / `EDINET_*` / etc. reads on the live path. Verified at static-graph level by `scripts/check_credential_isolation.py` (Task #88). |
| NFR-LIVE-BROKER-3 (dry-run parity) | `LiveBrokerAlpaca.execute()` first calls `PaperBroker.execute(action, prices)` and asserts `status == FILLED`; on any non-FILLED outcome the live submission is skipped and `BROKER_LIVE_REJECTED` is emitted. The PaperBroker becomes a mandatory pre-flight, not a backtest fixture. |
| NFR-LIVE-BROKER-4 (idempotency key) | Alpaca's native `client_order_id` is **48 chars max** (per Alpaca order-submission docs), but ADR-0008's recommended `sha256_hex(...)` is 64 chars. The Alpaca adapter MUST use the leading 48 hex chars of `sha256_hex((cycle_id, decision_run_id, ticker, side, quantity))` as the `client_order_id`. 192 bits of entropy still vastly exceeds collision risk; the venue-specific truncation is documented here per ADR-0008's risk-mitigation table ("a per-venue ADR will document venue-specific gaps"). The full 64-char key MUST be persisted alongside the venue-assigned `order_id` in the cycle event log so replay-vs-fresh disambiguation is recoverable post-hoc. |
| NFR-LIVE-BROKER-5 (kill-switch) | `LiveBrokerAlpaca.kill_switch()` performs three steps in order: (1) `DELETE /v2/orders` to cancel every open order, (2) `POST /v2/account/configurations` with `trade_suspended_by_user=true` to refuse new orders venue-side, (3) flip the local `_kill_switch_engaged: bool` so subsequent `execute()` calls return `SKIPPED` with reason `"kill switch engaged"`. The 1-cycle abort budget is met because `DELETE /v2/orders` is a single REST call (~100 ms typical) and step 3 is in-process. Re-enable requires the same human workflow as NFR-LIVE-BROKER-1. |
| NFR-LIVE-BROKER-6 (broker-level loss cap) | `LiveBrokerAlpaca.realized_loss_today_usd` accumulator, independent from the gateway's `daily_realized_loss_usd`. Configured via `live_broker_daily_loss_cap_usd` ctor arg (no env-var fallback — the cap is operational policy, not a credential). On breach, auto-engages `kill_switch()` and emits `BROKER_LIVE_KILL_SWITCH`. Day boundary follows ADR-0005's pattern: caller injects the day-boundary signal via `mark_start_of_day(today)`; no `datetime.now()` inside the broker. |
| NFR-LIVE-BROKER-7 (event taxonomy) | Maps Alpaca order-update webhook events to `CycleEventKind` members: `accepted` → `BROKER_LIVE_SUBMITTED`, `fill` / `partial_fill` → `BROKER_LIVE_FILLED` (one event per fill), `rejected` / pre-flight failure → `BROKER_LIVE_REJECTED`, `canceled` (venue-initiated or kill-switch-initiated) → `BROKER_LIVE_CANCELLED`, kill-switch engaged → `BROKER_LIVE_KILL_SWITCH`. `BROKER_EXECUTED` is never emitted by `LiveBrokerAlpaca`. |

## Decision drivers

- **Safety contract first, market coverage second.** ADR-0008's seven
  NFRs are the binding perimeter. Picking the venue that lets us land
  all four currently-xfailed assertions (NFR-LIVE-BROKER-3, -4, -5,
  -6) with the least venue-plumbing reduces the time the contract
  sits unproven. Alpaca's combination of paper API + native
  `client_order_id` + cancel-all primitive matches every NFR with
  off-the-shelf primitives.
- **Disposable proving ground.** Alpaca paper accounts are free and
  reset on demand; mistakes during P4 development cost nothing.
  Tachibana paper trading exists but requires Japanese tax-resident
  account paperwork; IBKR paper requires a funded live account or
  a separate sign-up. Friction matters when the goal is multiple
  iteration loops.
- **First-party Python SDK alignment.** `alpaca-py` is async-friendly
  and matches the existing CAQRS data-source SDK pattern (typed
  pydantic-style models, httpx under the hood). No bespoke transport
  layer. The IBKR official client (`ibapi`) is a thin wrapper over
  the TWS protocol and would require a transport rewrite.
- **Codex review concurrence.** Cross-family review independently
  picked Alpaca with the same reasoning. Two independent passes (the
  orchestrator's local analysis + Codex review) converging on the
  same venue is structural evidence the choice is robust to
  reviewer-side bias.

## Consequences

### Positive

- **Fastest path to flipped xfails.** The four currently-deferred
  assertions in `tests/test_broker_contract.py` (NFR-LIVE-BROKER-3,
  -4, -5, -6) flip in the first P4 PR; the lint side
  (NFR-LIVE-BROKER-2) flips when the new module's static graph
  passes the audit.
- **Concrete venue-mapping artifact.** The "Per-NFR mapping" table
  above is directly transcribable into the P4 PR's design notes; the
  contract suite's parametrize-shape (called out in PR-87's verifier
  report) accepts `LiveBrokerAlpaca` with one `pytest.param(...)`
  line.
- **Clear extension point.** ADR-0010 (Tachibana) inherits the
  per-NFR mapping table format from this ADR; venue-specific
  divergences become focused diffs rather than full rewrites.

### Negative

- **Japan-market coverage delayed.** J-Quants research-side decisions
  cannot be executed live until ADR-0010 lands. The research /
  execution loop is split across markets in the interim:
  research uses Polymarket + J-Quants signals; execution uses
  Alpaca on US tickers (or remains paper-only). This is acceptable
  because P4's goal is the safety contract proof, not market
  coverage — but operators must understand that a J-Quants
  `StrategyDecision` with `targets=("7203.T", ...)` cannot be
  executed by `LiveBrokerAlpaca`. The adapter MUST reject any
  ticker not in Alpaca's tradable universe (see "Out of scope"
  for the disposition).
- **Two adapters to maintain long-term.** Once ADR-0010 lands,
  CAQRS owns two `BrokerProtocol` implementations plus the paper
  one. The contract suite's parametrize-shape minimises the
  test-side cost; the maintenance cost is a real ongoing tax.
- **Idempotency truncation invariant.** The 48-char client-order-id
  truncation is a venue-specific deviation from ADR-0008's
  recommended 64-char form. Documented here, but a future
  Alpaca-side change (longer `client_order_id` support, or stricter
  format requirements) would force this ADR to be amended.

### Risks (and mitigations)

| Risk | Mitigation |
| --- | --- |
| 48-char truncation collision across cycles with identical `(cycle_id, decision_run_id, ticker, side, quantity)` tuples | The tuple already uniquely identifies a logical order per ADR-0008 NFR-LIVE-BROKER-4. Truncating from 256 bits to 192 bits leaves the collision probability negligible (>10²⁸ orders before 1% birthday-bound collision). The full 64-char key is persisted alongside `order_id` so replay disambiguation is post-hoc recoverable. |
| Alpaca API rate limits trip kill-switch's `DELETE /v2/orders` step | Alpaca's documented rate limit is 200 req/min on the trading API. The kill-switch path is an N+2-call sequence (cancel-all + suspend-config + emit event); single-cycle bound is preserved. If Alpaca returns 429, the adapter MUST log the failure and **still** emit `BROKER_LIVE_KILL_SWITCH` with reason `"venue cancel-all rate-limited; manual intervention required"` — the local refusal-of-new-orders state matters more than the venue-side cancellation success. |
| Alpaca webhook delivery is at-least-once | NFR-LIVE-BROKER-7's event taxonomy already accepts that partial fills emit one event per fill; duplicate webhook delivery becomes a duplicate `BROKER_LIVE_FILLED` event. The cycle event log treats events as facts; downstream PnL accounting must dedupe by `order_id + fill_id`. This is a paper-broker-vs-live-broker divergence — paper emits exactly one event per fill — and an explicit P4 test covers it. |
| `LIVE_BROKER_BASE_URL` defaults to paper, but operator overrides to live without realising | The two-step approval workflow (NFR-LIVE-BROKER-1) plus the broker-side default-off (NFR-LIVE-BROKER-1) are the binding gates; `LIVE_BROKER_BASE_URL` is informational. The CLI `confirm-live` step MUST display the resolved base URL and ask for explicit y/N confirmation. |

## Implementation checklist (for the future P4 PR)

- [ ] `LiveBrokerAlpaca` class implementing `BrokerProtocol` and the
      seven NFRs above, residing in
      `caqrs/execution/live_broker_alpaca.py`
- [ ] Promote `caqrs.execution.live_broker` boundary in
      `caqrs.lint.credential_isolation.DEFAULT_BOUNDARIES` to
      `caqrs.execution.live_broker_alpaca`
- [ ] `alpaca-py` added as an optional dependency under
      `[project.optional-dependencies] live-broker = ["alpaca-py>=0.x"]`
      (the live-broker code path remains pip-extra-gated so the
      paper-only deployments don't pull SDK deps)
- [ ] CLI `python -m caqrs.execution.live_broker_alpaca confirm-live`
      for the NFR-LIVE-BROKER-1 two-step approval
- [ ] Contract suite parametrize: add a `pytest.param(LiveBrokerAlpaca, id="LiveBrokerAlpaca")` to the broker-contract fixture; the four currently-deferred xfail markers (NFR-LIVE-BROKER-3, -4, -5, -6) flip to passing
- [ ] LICENSE_AND_TOS.md row for Alpaca's API ToS; the TOS lint
      gate (`scripts/check_data_source_tos.py`) catches the
      omission
- [ ] Verifier report per ADR-0007 trigger 6 (live-broker, regardless of
      size); cross-family + human-gate escalation per ADR-027
- [ ] Two-step TDD dispatch per ADR-0006; Step 1 commits the
      contract-suite parametrize change with the four flipped xfails
      now passing against a `LiveBrokerAlpaca` whose body raises
      `NotImplementedError`, Step 2 implements

## Out of scope

- **The `LiveBrokerAlpaca` class itself.** This ADR selects the
  venue and maps the safety NFRs against Alpaca's primitives; the
  implementation lands in P4 (the next slice in this sequence).
- **Tachibana / Rakuten / IBKR adapters.** Tachibana is reserved for
  ADR-0010 once the ADR-0009 implementation proves the internal
  `BrokerProtocol` contract end-to-end. IBKR is not currently on
  the roadmap; if a future requirement (multi-venue routing, a
  partner's existing IBKR account) arises, ADR-0011+ would address
  it.
- **Market-coverage routing.** ADR-0009 does not specify how a
  J-Quants `StrategyDecision` is routed when the ticker is not in
  Alpaca's tradable universe. The simplest disposition is to
  reject at the gateway-side (extend `PolicyGatewayConfig` with an
  `executable_universe` set per active broker); ADR-0010's landing
  is the natural moment to revisit.
- **Modification of ADR-0008's NFRs.** No NFR is changed by this
  ADR; the per-NFR mapping above shows how Alpaca satisfies each.
  If a future Alpaca-side change forces an NFR amendment, that
  amendment is a separate ADR.

## Reconsider when

- **Tachibana ADR-0010 lands and the per-venue ADR pattern needs
  refactoring.** The first time we have two venue ADRs we will know
  whether the per-NFR mapping table belongs in each per-venue ADR
  or in a shared appendix.
- **Alpaca deprecates `client_order_id` or changes its length
  limit.** The 48-char truncation rule is venue-specific and would
  need amendment.
- **The Codex audit (or a future audit) downgrades Security or
  Reliability scores on a deployed P4 stack.** The seven NFRs
  themselves stay binding; the venue mapping may need tightening
  beyond what is documented here.
- **A future ADR tightens NFR-LIVE-BROKER-3 to require partial-fill
  pre-flight equivalence (not just FILLED equivalence).** The
  current Alpaca mapping treats partial fills as live-only events;
  a stricter NFR would require additional adapter logic.
