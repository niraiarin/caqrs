# ADR-0007: Verifier reports as committed audit artifacts

- **Status**: Accepted
- **Date**: 2026-05-02
- **References**: Codex GPT-5.5 audit of the CAQRS pipeline (April–May 2026); agent-manifesto ADR-022 (Verifier role) and ADR-027 (cross-/intra-family verifier policy v0.1.0)
- **Cross-references**: ADR-0006 (two-step TDD dispatch — referenced as the upstream of this convention; see "Out of scope" if not yet authored)

## Context

CAQRS development since P3.a has used a multi-agent loop in which
Claude Opus 4.7 orchestrates and a Codex GPT-5.5 instance reviews
each implementation diff before merge. Per agent-manifesto ADR-027
v0.1.0, **intra-family** verification (both implementor and verifier
are Codex GPT-5.5 instances, with Claude as a cross-family
orchestrator on top) is acceptable for paper-broker scope; live-broker
scope (P4+) escalates to cross-family + human-gate.

The verifier role is well-defined; the **artifact** it produces is
not. Reviews of PRs #71 (P3.d-4 broker wiring), #72 (Phase E1
in-memory EntityStore), and #73 (Phase E2 DuckDB EntityStore) all
happened — with concrete findings, severity classifications, and
disposition decisions — but the records exist only in conversation
transcripts outside the repository, and squash-merge commits preserve
the implementor's PR body but not the verifier's review.

The Codex GPT-5.5 audit of the pipeline scored this step **2/5** with
the comment *"this is process-level, not machine-checkable in the
repo."* The auditor could see that ADR-027 mandates a verifier and
could see PRs landing, but could not verify that any particular PR
had actually been reviewed.

The orchestrator already produces a structured implementation brief
per PR (objective, type contract, test list, gates). A symmetric
verifier report belongs alongside it, in git, for the same reason:
auditability must survive the conversation that produced it.

## Decision

For every PR matching ANY of the triggers below, the merging branch
must include `docs/reviews/PR-<n>.md` populated from
`docs/reviews/_template.md` **before merge**.

### Triggers (any one is sufficient)

1. **Risk classification ≥ medium** — changes that touch security,
   broker, policy gateway, or persistence boundaries.
2. **New module addition** — e.g. `caqrs.entities` (Phase E1, PR #72),
   `caqrs.entities.duckdb` (Phase E2, PR #73). New top-level packages
   and new top-level modules under an existing package both qualify.
3. **Type-contract changes** to `PolicyGatewayConfig`, `BrokerProtocol`,
   or the `EntityStore` Protocol.
4. **ADR introduction** — every new file under `docs/decisions/`.
5. **Size threshold** — any PR adding ≥ 200 lines of code (excluding
   `tests/`, `docs/`, lockfiles).
6. **Live-broker (P4+) PRs** — always required, regardless of size or
   the other triggers, and additionally subject to the cross-family +
   human-gate escalation in ADR-027.

PRs that match none of the triggers (typo fixes, dependency bumps with
no API surface change, documentation-only PRs to `docs/research/`) do
not require an artifact. The implementor may still produce one
voluntarily if the diff is judgment-laden.

### Artifact shape

`docs/reviews/_template.md` is the canonical template. Each report
records:

- review date
- verifier identity (model + role — e.g. `Codex GPT-5.5 (intra-family
  per ADR-027)`, `Claude Code Opus 4.7 (general-purpose subagent)`,
  `Human reviewer: <name>`)
- `<base>..<head>` SHAs being reviewed
- PR title and scope statement
- the exact gate commands the verifier ran (or could not run)
- findings table with severity (`blocker` / `major` / `minor`
  / `nitpick`), description, and disposition (`accepted` /
  `fixed-before-merge` / `follow-up issue #N` / `wontfix`)
- final verdict (`APPROVE` / `APPROVE_WITH_NITS` / `REQUEST_CHANGES`)
- an explicit **Unchecked risks** section naming what the verifier
  could not check given the available sandbox / tooling

The template is deliberately small (≤50 lines) so that filling it out
is cheap; the cost is dominated by the review itself, not the writeup.

### Backfill

PRs #71–#73 match the triggers (broker wiring, new module, new module)
but predate this convention. Their reports are backfilled in this same
commit, reconstructed from PR body, merge commits, and the
orchestrator's transcript-recovered review notes. Where the transcript
is incomplete, the report says so explicitly rather than inventing a
finding. PRs #67–#70 are **not** backfilled — the intra-family
verifier protocol was not in force at the time, and retroactive
reviews would weaken the audit trail rather than strengthen it.

## Decision drivers

- **Symmetry with the implementation brief.** The orchestrator already
  drafts a brief (type contract, test list, gates) that lands in git
  via the PR body and relevant ADR / spec. The review is the dual:
  same shape, opposite direction. Keeping only the implementor's half
  of the pair is asymmetric and loses exactly the auditability the
  brief was added for.
- **Audit reproducibility.** A future reader (Codex audit, human
  contributor, automated traceability check) must answer "was PR #N
  reviewed, by whom, against what gates, with what findings?" without
  replaying any conversation. The artifact makes that a `cat` / `grep`
  operation.
- **Honesty about gaps.** Sandbox-restricted verifiers (e.g. read-only
  Codex GPT-5.5 instances that cannot run pytest or hit the network)
  must record what they *could not* check. Without an artifact, the
  absence is invisible. PR #73 is the example: no independent verifier
  review actually happened, only orchestrator self-review; the
  backfilled report says so explicitly rather than fabricating a Codex
  pass.
- **Lightweight by design.** 50–200 lines per qualifying PR. No CI
  enforcement yet, no mandatory follow-up tooling. The artifact is the
  deliverable; everything else is downstream.

## Consequences

### Accepted

- Verifier identity, model, gate commands, findings, and disposition
  become auditable in git for every qualifying PR.
- Future tasks that depend on review-trail integrity (e.g. a contract
  test suite, P4 reconciliation, the live-broker promotion checklist)
  inherit the discipline by default.
- The PR template / contributor docs (forthcoming) will reference the
  trigger list above so external contributors know when to attach a
  report.

### Costs

- One markdown file per qualifying PR (~50–200 lines).
- Mild duplication with the PR body's "Review notes" section, by
  design: the PR body is the implementor's writeup, the report is the
  verifier's. They overlap in summary but diverge in scope.

### Risks (and mitigations)

| Risk | Mitigation |
| --- | --- |
| Implementor self-reviews without an independent verifier | ADR-027 cross-family + human-gate rule for live-broker PRs; for paper scope, the report's **Verifier** field must name a different model than the implementor or explicitly say "self-review (orchestrator)" so the gap is visible |
| Reports become a checkbox ritual | Severity / disposition columns force the verifier to commit to a position rather than narrate; **Unchecked risks** forces honesty about sandbox limits |
| Report drifts from the actual review | The artifact is the **single source of truth**; if a verdict is recorded as `APPROVE_WITH_NITS` and a finding is `accepted`, the merge proceeds on that basis. Conversation transcripts are not authoritative once the artifact lands. |

## Out of scope

- **CI enforcement of the artifact's presence** — a separate task adds
  a pre-merge check that fails if a triggering PR lacks the report.
  Until that lands, enforcement is reviewer-driven.
- **Backfilling PRs #67–#70** — see Decision above.
- **ADR-0006 cross-reference** — referenced as the upstream
  convention; if ADR-0006 has not yet been authored, treat as a
  forward pointer. The trigger list here stands on its own.

## Reconsider when

- A CI artifact-presence check lands and the trigger list needs
  mechanised expression (labels, file globs, …).
- The verifier population grows beyond the Codex / Claude / human
  trio; the **Verifier** field may need a controlled vocabulary.
- A live-broker (P4+) PR ships and the cross-family + human-gate
  escalation surfaces gaps in this ADR's coverage.
