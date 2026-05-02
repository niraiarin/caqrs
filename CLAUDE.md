# CLAUDE.md

Project-local guidance for Claude Code (and other agents) operating on this
repository. Inherits the user-global TyDD + TDD discipline from
`~/.claude/CLAUDE.md`; the rules below are CAQRS-specific overrides and
additions. Where they conflict with the global file, this file wins.

## TDD discipline

CAQRS follows the user-global TyDD + TDD reference (failing example first,
type holes drive design, pure-function-first decomposition). The repo-local
addition is the dispatch protocol below, which makes the red phase auditable
under squash-merge.

### Two-step TDD dispatch (ADR-0006)

Any PR that introduces new functional surface (new module / class / public
method) must follow:

1. **Step 1**: Author failing tests with
   `@pytest.mark.xfail(strict=True, reason="impl pending — Task #N")`. Pydantic
   types and protocol stubs may be added only to the extent the test module
   needs to import. Run `uv run --frozen pytest -q tests/<new_file>` and paste
   the output into the PR body. Commit message:
   `test(<scope>): failing test list for <REQ-IDs> (TDD step 1)`. Push to the
   slice branch; do not merge to `main` yet.
2. **Step 2**: Implementation flips the `xfail` markers to pass. Each marker
   is removed in the commit that makes the corresponding test green. The final
   commit must have **0 remaining `xfail` markers in the new tests**. Commit
   message: `feat(<scope>): implement <REQ-IDs> (TDD step 2)`. Squash-merge to
   `main`.

`@pytest.mark.skip` is **forbidden** in this protocol — only
`xfail(strict=True)` produces auditable evidence.

**Escape hatch**: trivial single-case fixes (≤1 test added, no new module /
class / public method) MAY waive Step 1 with the commit footer:

```
TDD-waiver: trivial single-case fix; no separate failing-test commit
```

The verifier rejects this footer on any PR that adds two or more tests or any
new functional surface.

See `docs/decisions/0006-two-step-tdd-dispatch.md` for full rationale and the
risk-mitigation notes (shallow-assertion rejection, fake-step-1 detection).
