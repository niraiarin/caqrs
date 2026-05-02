# ADR-0006: Two-step TDD dispatch (failing tests first, then implementation)

- **Status**: Accepted
- **Date**: 2026-05-02
- **References**: Codex GPT-5.5 intra-family verifier audit; orchestrator
  retrospective on PR #71 (P3.d-4 paper-broker wiring), #72 (EntityStore
  P1), #73 (EntityStore DuckDB P2). Cross-references ADR-022 / ADR-027 of
  the `agent-manifesto` plugin (Verifier role, intra-family review).

## Context

CAQRS develops in a TyDD + TDD style (see `~/.claude/CLAUDE.md` § "TyDD +
TDD 運用" and `docs/research/data-integration/02-design-spec-tydd.md`).
The discipline calls for a failing example before any implementation.

The retrospective on #71 / #72 / #73 surfaced a structural gap:

- **Squash-merge collapses the red→green sequence.** This repo squashes
  every PR to a single commit on `main`. Even when the implementor agent
  ran `pytest` against a failing test list before writing implementation,
  the merged commit shows tests and code together. Git history alone
  cannot prove the test was ever red.
- **Same-response authoring.** Codex and Claude Code both default to
  emitting tests + implementation in the same response when handed a
  feature slice. The natural model output is "here is the spec, here are
  the tests, here is the code that passes them" — which is plausibly
  TDD-shaped without being TDD.
- **Verifier sees only the final diff.** The intra-family Codex
  verifier (per `agent-manifesto` ADR-027) reviews the slice as a whole,
  not the working-tree timeline. It cannot distinguish "tests written
  first, run red, then implementation flipped them green" from "tests
  authored to pass the implementation that was written alongside them".
  The verifier's pipeline-step score for #71 was 2/5 on this axis.
- **TDD claims become unfalsifiable.** Without an artifact that pins the
  red phase, "we did TDD" is a self-report, not an audit trail.

The fix has to (a) leave physical evidence of the red phase in artifacts
the verifier *can* see (PR body + xfail markers), and (b) be cheap enough
that implementor agents will actually follow it.

## Decision

Mandate a **two-step dispatch** for any PR that introduces new functional
surface — a new module, a new public class, or a new public method. The
two steps live on the slice branch as separate commits; the PR squashes
to `main` as usual but the per-step evidence is preserved in the PR body
and in `xfail` markers.

### Step 1 — Failing tests + types only

The implementor authors:

- The test list for the slice (REQ-IDs from
  `docs/requirements/registry.yaml` covered, given/when/then per case).
- Pydantic types and protocol stubs **only** to the extent required for
  the test module to import. No method bodies beyond `raise
  NotImplementedError` or `...`.
- Each new test is decorated:

  ```python
  @pytest.mark.xfail(strict=True, reason="impl pending — Task #N")
  def test_<scope>_<behaviour>() -> None:
      ...
  ```

  `@pytest.mark.skip` is **forbidden** — skipped tests are invisible to
  `pytest -q` and defeat the audit purpose.

- The implementor runs `uv run --frozen pytest -q tests/<new_file>` and
  pastes the output into the PR body under a `## Step 1: failing tests`
  heading. The output must show `xfailed` (not `xpassed`) for every new
  test — `strict=True` turns an unexpectedly-passing xfail into a hard
  failure, which is what proves the assertion would actually be red
  without the marker.

Commit message:

```
test(<scope>): failing test list for <REQ-IDs> (TDD step 1)
```

Step 1 is pushed to the slice branch. It is **not** merged to `main`.

### Step 2 — Implementation flips xfail to pass

The implementor adds the production code. As each test starts passing,
its `xfail` marker is removed in the same commit that makes it pass.
The final commit on the slice branch must contain **zero remaining
`xfail` markers in the new tests** (existing `xfail` elsewhere in the
suite is untouched — see "Out of scope" below).

Commit message:

```
feat(<scope>): implement <REQ-IDs> (TDD step 2)
```

The PR squash-merges to `main`. The PR body retains the Step 1 pytest
output, so post-merge auditors (human or Codex verifier) can still see
the red phase even though git history collapses it.

### Escape hatch — trivial single-case fix

For PRs that add **at most one test** (e.g. a single bug-reproduction
+ fix, a one-line clarification of an existing assertion), the
implementor MAY skip Step 1 by adding a footer to the commit message:

```
TDD-waiver: trivial single-case fix; no separate failing-test commit
```

The verifier MUST reject this footer if the PR adds:

- two or more new tests, or
- any new module / public class / public method.

The waiver is for the case where the cost of a separate red commit is
genuinely larger than the audit value of having one.

## Consequences

### Positive

- **Failing-test-first becomes auditable.** Two evidence types — the
  `xfail(strict=True)` markers in Step 1 and the pasted pytest output in
  the PR body — let the verifier confirm the red phase without trusting
  the implementor's self-report. Either alone is forgeable; both
  together close the loop.
- **The orchestrator can dispatch independently.** Step 1 and Step 2
  become separate prompt slots. Step 1 can be assigned to one agent and
  Step 2 to another (cross-family by default), which mirrors the
  Implementor / Verifier split per ADR-022 of `agent-manifesto`.
- **Sharper review focus.** The verifier reviews Step 1 specifically for
  test quality (assertion strength, edge-case coverage, REQ-ID linkage)
  and Step 2 for implementation quality. Mixing the two reviews in one
  pass — as today — reliably under-weights the test-quality side.

### Negative

- **PR overhead.** Each slice gains ~5–10 minutes for the extra commit,
  the extra `pytest` run, and the PR-body update.
- **Squash-merge still loses granular history.** This ADR does not
  attempt to abandon squash-merge (the alternative — preserving merge
  commits — was rejected in earlier discussion as more disruptive than
  the audit gain warrants). The audit value comes from the PR body and
  the `xfail` markers, not from `git log` per se.

### Risk — fake step 1

An implementor agent could fake Step 1 by writing trivially-passing
assertions (`assert True`, `assert isinstance(x, object)`) under
`xfail(strict=True)`, then "discovering" the real assertion in Step 2.
The `strict=True` marker would still trigger `xpassed` and make the
test fail, but the assertion content is shallow.

**Mitigation**: the verifier reviews the Step 1 commit specifically and
rejects shallow assertions. Task #82 will produce a verifier artifact
(step-1 audit checklist) that codifies the rejection criteria
(assertion specificity, REQ-ID coverage, no `assert True` / no
`isinstance(_, object)` patterns).

## Out of scope

- **Retrofitting existing tests with `xfail`.** Task #80 is the
  forward-fix on traceability; backfilling `xfail` to old tests would
  defeat this ADR's purpose (`xfail` only proves a red phase if it was
  red *at the time* — markers added after the fact prove nothing).
- **CI enforcement of "Step 1 commit must exist".** A CI gate that
  inspects the slice branch's commit graph is technically possible but
  out of scope here; the discipline is enforced by the verifier in
  v0.1.0. A CI gate may be added later if the verifier's miss rate is
  measurable.
- **Property-based tests.** Hypothesis tests under `xfail(strict=True)`
  behave the same way (any single example passing causes `xpassed`),
  but the assertion-shallowness mitigation is harder. Step 1 may use
  example-based tests even where the eventual surface will be
  property-based; Step 2 promotes them.

## Implementation checklist

- [ ] `CLAUDE.md` (this repo) updated with the two-step dispatch
      protocol under the TDD section.
- [ ] Future Codex / Claude Code briefs reference this ADR by number
      when dispatching new-feature slices.
- [ ] Task #82 (verifier artifact) cross-references this ADR for the
      step-1 audit checklist (assertion specificity, REQ-ID coverage,
      shallow-assertion rejection criteria).
