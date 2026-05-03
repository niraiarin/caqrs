# Verifier Report — PR #NEXT (Task #87 broker contract test suite)

**Reviewed at**: 2026-05-02
**Verifier**: Claude Code Opus 4.7 (general-purpose subagent — same family as the implementor; honest disclosure per ADR-0007)
**Diff base..head**: `4da90df..HEAD` on `feat/broker-contract-test-suite` (replace HEAD with the squash-merge SHA on landing)
**PR title**: test(execution): broker contract test suite for NFR-LIVE-BROKER-1..7 (Task #87)

> NB: this PR's number is not yet assigned. Rename `PR-NEXT.md` to
> `PR-<n>.md` after `gh pr create` returns.

## Scope

Reviewed: the new parametrized contract test suite
(`tests/test_broker_contract.py`), the registry status flips for
NFR-LIVE-BROKER-1..7 (`docs/requirements/registry.yaml` v7 → v8), and
the two-step TDD dispatch evidence (Step 1 commit + Step 2 commit).

Both ADR-0006 (two-step TDD dispatch) and ADR-0007 (verifier artifact)
apply; both are honored. ADR-0008 (live-broker safety perimeter) is
the spec the contract suite asserts against.

## Commands run

```bash
uv run --frozen ruff format --check .         # All files formatted
uv run --frozen ruff check .                  # All checks passed!
uv run --frozen mypy src                      # Success: 95 source files
uv run --frozen mypy tests                    # Success
uv run --with pyyaml python scripts/check_traceability.py
                                              # OK; 195 unique REQ-IDs; 113 traces
uv run --frozen pytest tests/test_broker_contract.py -q
                                              # Step 1: 8 xfailed
                                              # Step 2: 4 passed, 4 xfailed
uv run --frozen pytest -q
                                              # 829 passed, 12 deselected, 5 xfailed
```

## Two-step TDD evidence (ADR-0006)

- **Step 1 commit**: `455251e test(execution): broker contract test
  suite for NFR-LIVE-BROKER-1..7 (TDD step 1)`
  - Added `tests/test_broker_contract.py` with 8 tests, each
    decorated `@pytest.mark.xfail(strict=True, reason="...")`. Test
    bodies for NFR-1, NFR-2 (×2), and NFR-7 raise
    `NotImplementedError("Task #87 step 1 placeholder; assertion
    authored in step 2")`. Test bodies for NFR-3, -4, -5, -6 raise
    `NotImplementedError("LiveBroker ... assertion deferred to P4
    PR")` (these are the LiveBroker-only NFRs).
  - pytest output: `8 xfailed in 0.06s` — clean red phase.
- **Step 2 commit (this commit)**: removes `xfail` for NFR-1, NFR-2
  (×2), and NFR-7 (PaperBroker satisfies them) and authors the real
  assertion bodies. NFR-3, -4, -5, -6 keep `xfail` with a documented
  reason; their bodies are upgraded from bare `NotImplementedError`
  to the *probing* assertion the LiveBroker PR will satisfy
  (`getattr(broker, "compute_idempotency_key", None)` etc.) so the
  flip in P4 is one line — remove `xfail`, no body change.
  - pytest output: `4 passed, 4 xfailed in 0.10s`.

The PR body should retain the step-1 pytest output verbatim so
post-squash auditors can see the red phase.

## Per-NFR status (post-step-2)

| NFR | Test name | Status | Registry status |
|---|---|---|---|
| NFR-LIVE-BROKER-1 | `test_default_off_for_live_brokers_only` | passed | partial |
| NFR-LIVE-BROKER-2 | `test_paper_broker_does_not_import_live_broker_env_vars` | passed | partial |
| NFR-LIVE-BROKER-2 | `test_broker_does_not_leak_credentials_across_classes` | passed | partial |
| NFR-LIVE-BROKER-3 | `test_dry_run_does_not_change_broker_state` | xfailed (LiveBroker only) | deferred |
| NFR-LIVE-BROKER-4 | `test_idempotency_key_is_deterministic` | xfailed (LiveBroker only) | deferred |
| NFR-LIVE-BROKER-5 | `test_kill_switch_aborts_within_one_cycle` | xfailed (LiveBroker only) | deferred |
| NFR-LIVE-BROKER-6 | `test_broker_level_daily_loss_cap_independent_from_gateway` | xfailed (LiveBroker only) | deferred |
| NFR-LIVE-BROKER-7 | `test_paper_broker_uses_broker_executed_not_broker_live_kinds` | passed | partial |

Three NFRs flip from `deferred` → `partial`; four stay `deferred`
(test exists, awaits LiveBroker). Registry version bumped 7 → 8.

## Findings

| Severity | Description | Disposition |
|---|---|---|
| minor | The Step 1 commit's NFR-1/-2/-7 test bodies were `raise NotImplementedError(...)` rather than the eventual real assertion. ADR-0006 §"Risk — fake step 1" calls out shallow assertions; in this case the *real* assertion appears in Step 2 (verifier can diff), so the shallow-step-1 risk is bounded. The alternative — writing the real assertion in Step 1 with `xfail(strict=True)` — would have caused `xpassed` failures because PaperBroker satisfies the NFR. The chosen approach is therefore the only mechanically valid Step 1 for tests-only PRs against an *existing* implementation. | accepted |
| minor | The fixture is parametrized over a single broker (`PaperBroker`). The brief mandates the parametrize-shape so LiveBroker can be added with a single `pytest.param(...)` line; verified by inspection but not exercised today. | accepted |
| minor | The NFR-2 `test_broker_does_not_leak_credentials_across_classes` static check uses `inspect.getsource(inspect.getmodule(type(broker)))`. For PaperBroker this reads `src/caqrs/execution/paper_broker.py`. For a future LiveBroker that lives in (e.g.) `src/caqrs/execution/live_broker.py`, the same call resolves to its module — generic. There is no separate test for "the broker's *imported transitive* modules don't leak credentials"; that is out of scope (the credential-isolation static-import audit per NFR-2's measurement field is a dedicated lint task — Task #88 — not the contract-suite responsibility). | accepted |
| minor | The NFR-7 test re-implements the CycleRunner happy-path wiring already covered by `tests/test_orchestrator_paper_broker.py::test_adopt_fills_and_emits_filled_event`. The duplication is intentional: NFR-7's assertion is the *taxonomy* (`BROKER_LIVE_*` absence, both by enum-value-prefix and by enum-name-prefix), which the orchestrator test does not cover. The shared scaffolding (helper builders) is duplicated rather than extracted to a `conftest.py` to keep the contract suite self-contained — easier to read in isolation, no cross-file coupling. | accepted |
| minor | The Step 2 LiveBroker-only test bodies (NFR-4, -5, -6) include the *probing* assertion (`getattr(broker, "compute_idempotency_key", ...)`) so when LiveBroker lands the flip is `xfail` removal only. NFR-3 stays as `raise NotImplementedError` because it has no obvious probing form (dry-run parity is a behavioural property, not an attribute check). The asymmetry is documented in the test docstring. | accepted |
| nitpick | `yq -i` mutated comment placement on the registry section break (the `# LIVE-BROKER SAFETY (P4 prerequisite)` comment block was originally between two REQ entries but yq's structural model attached it to NFR-LIVE-BROKER-1's `id` line). Step 2 includes a small Python script to restore the comment to its pre-yq location; the diff is structurally identical otherwise. | fixed-before-merge |

## Spec deviations (declared)

None. The contract suite asserts NFRs as written in ADR-0008. The
xfail markers honour ADR-0006's two-step dispatch. The registry
amendments populate `tests[]` fields exactly as ADR-0008's "Out of
scope" note anticipated.

## Verdict

**APPROVE_WITH_NITS** — the contract suite implements the seven NFRs
from ADR-0008 in a parametrized form ready for LiveBroker addition,
the two-step TDD discipline (ADR-0006) is honoured with verifiable
red-phase evidence in commit `455251e`, the registry is updated for
the three NFRs PaperBroker actually satisfies (status `partial`) and
the four that genuinely need LiveBroker (status `deferred` with a
note that the test exists and flips on LiveBroker landing), and all
gates pass. The cross-family Verifier role per ADR-0007 / agent-
manifesto ADR-022 is **not** satisfied (this is a same-family Claude
self-review); the orchestrator should arrange a Codex GPT-5.5
follow-up review before merge if the PR is risk-classified ≥ medium
per ADR-0007's trigger list. The PR matches trigger 5 (≥200 LOC) so
the cross-family follow-up is recommended.

## Unchecked risks

- **Same-family verifier**: this report is by Claude Code Opus 4.7,
  same family as the implementor (also Claude Code Opus 4.7 in this
  session). Per ADR-0007 the cross-family option is preferred; for
  paper-broker-scope contract tests it is acceptable to mark as
  self-review. This report is honest about that gap.
- **LiveBroker not yet implemented**: the four xfail tests
  (NFR-3, -4, -5, -6) are not exercised against a real LiveBroker —
  they document the contract surface but cannot prove a hypothetical
  implementation would satisfy it. The flip in P4 will reveal any
  gap in the assertion; the verifier of that PR will evaluate
  "is the assertion strong enough?".
- **NFR-2 transitive-import leakage**: the static check inspects only
  the broker's own module source. A LiveBroker that *imports* a
  helper which reads `JQUANTS_*` env vars would not be caught by the
  contract suite. Task #88 (dedicated lint) is the proper coverage
  for that; the contract suite does not pretend to.
- **NFR-7 partial-fill / cancellation events**: the assertion checks
  the *happy path* only (one cycle, FILLED). Partial fills or
  cancellations on a future LiveBroker would emit
  `BROKER_LIVE_FILLED` / `BROKER_LIVE_CANCELLED`; those event kinds
  are not in `CycleEventKind` today, so the assertion's
  "no `BROKER_LIVE_*` value-prefix or name-prefix in `kinds`" is
  vacuously true. When LiveBroker lands and the enum gains those
  members, the negative assertion remains correct (PaperBroker still
  doesn't emit them). No code change to the contract suite needed.
- **Registry comment-placement**: the small Python script that
  restored the yq-perturbed comment block is one-shot; future yq
  edits will perturb it again. A follow-up could move the section
  comment into a top-level field (e.g. a `_section_comments` map) but
  that is out of scope for Task #87.
