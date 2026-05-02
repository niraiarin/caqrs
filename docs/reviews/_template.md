# Verifier Report — PR #<n>

**Reviewed at**: 2026-MM-DD
**Verifier**: <model-name + role; e.g., "Codex GPT-5.5 (intra-family per ADR-027)" or "Claude Code Opus 4.7 (general-purpose subagent)" or "Human reviewer: <name>">
**Diff base..head**: `<base-sha>..<head-sha>`
**PR title**: <copy from gh pr view>

## Scope

<1-2 sentences on what was reviewed: spec / impl / both>

## Commands run

```bash
<exact gates the verifier ran>
```

## Findings

| Severity | Description | Disposition |
|---|---|---|
| blocker / major / minor / nitpick | what was found | accepted / fixed-before-merge / follow-up issue #N / wontfix |

## Verdict

APPROVE / APPROVE_WITH_NITS / REQUEST_CHANGES

## Unchecked risks

<things the verifier could not check given the sandbox / tooling, e.g., "could not run pytest in read-only sandbox" or "live API behavior not verified">
