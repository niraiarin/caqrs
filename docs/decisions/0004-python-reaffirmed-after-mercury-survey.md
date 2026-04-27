# ADR-0004: Python reaffirmed as the implementation language after the Mercury survey

- **Status**: Accepted
- **Date**: 2026-04-27
- **Supersedes**: extends ADR-0001 (independent repo) with a re-validation of the language choice

## Context

ADR-0001 chose Python for CAQRS based on a high-level argument
("financial-research stack does not cross-compile from TypeScript").
After completing the Mercury Agent Harness survey
(`docs/research/mercury-survey/`, 4,593 lines of Mercury source read,
44 importable patterns identified) and shipping seven PRs
(P0 + P1.0 + P1.1.a/b/c.1/c.2/d + P1.1.5 — provider layer complete),
the question was raised whether Python is still the right choice.

A re-evaluation against the current evidence base was performed
covering Python, TypeScript, Rust, Go, Julia, and Python+Rust hybrid
configurations.

## Decision

CAQRS continues in **Python 3.12+** with `pydantic v2` strict, `mypy`
strict, and `httpx` async. The Mercury survey's 44 importable
patterns will be ported to idiomatic Python during P1.2 through P3.

## Decision drivers (ranked by impact)

### 1. P2 backtest stack is Python-dominant (decisive)

CAQRS P2 (backtest harness) targets walk-forward, statistical tests,
regime classification, and signal computation. The Python ecosystem
(`pandas`, `vectorbt`, `statsmodels`, `scipy`, `numpy`,
`scikit-learn`) has decades of investment with no cross-language
equivalent of comparable maturity.

- TypeScript: no research-grade backtester or statistical library.
- Rust: `polars` is strong for dataframe work, but `statsmodels` /
  `scipy` equivalents do not exist; would require self-implementation
  of standard tests.
- Julia: closest scientific-computing alternative but smaller
  ecosystem and weaker static-typing posture (`pydantic + mypy`
  combined).

A non-Python implementation would either (a) reach P2 and stall, or
(b) require a Python sub-process for analysis — either negates the
benefit of the language switch.

### 2. Sunk cost in subscription-credential reuse (significant)

P1.1.a/b/c.1/c.2/d ports OpenClaw's CLI-credential-reuse paths into
working Python with 135 tests. Switching languages forces:

- 1-2 weeks rewrite of the provider layer
- Re-validation of ToS compliance for the Anthropic / Codex
  subscription paths
- Loss of the per-error-type tests currently pinning behaviour

The cost is non-trivial relative to the benefit of any alternative.

### 3. Mercury patterns are language-agnostic at the design level

The survey's 44 patterns translate to algorithms, schemas, and
state-machine definitions. Implementation language is incidental.
Specifically:

- **ToolCallLoopDetector** (240 TS lines) → ~150 Python lines, pure
  data-structure work.
- **Second Brain** (1075 TS lines) → ~800-1000 Python lines using
  `sqlite3` + FTS5 (stdlib), pydantic for record types.
- **Permission gateway** patterns → straightforward dataclass /
  pydantic translations.

Mercury's TypeScript was driven by Vercel AI SDK + Ink TUI + grammY
(see ADR-005, ADR-002, ADR-004). CAQRS uses none of these — the
language pull is correspondingly weaker.

### 4. Type safety is sufficient, not maximal

`mypy --strict` + `pydantic v2` + `frozen=True` + `extra=forbid` +
PEP 695 generics achieves roughly 85-90% of TypeScript's static
guarantees. The remaining 10-15% (parametric variance, exhaustive
match assertions) is covered by property-based tests (Hypothesis).

Rust would push to 95-99%, but at a development-velocity cost
incompatible with research-prototype iteration cadence (compile
times, borrow-checker friction on async code).

### 5. AI-assisted development quality is comparable

Claude Opus 4.7 produces high-quality Python and TypeScript with
similar fidelity. Rust quality is good but more verbose to
specify. Go quality is acceptable for infrastructure but weaker for
domain modelling. No language has a decisive AI-assist advantage
over Python.

## Re-evaluation table

| Criterion | Python | TypeScript | Rust | Weight |
| --- | :---: | :---: | :---: | :---: |
| 1. Quant research stack (pandas / vectorbt / statsmodels / scipy) | ★★★★★ | ★ | ★★ | **decisive** |
| 2. AI SDK / LLM ecosystem | ★★★★ (httpx self-rolled) | ★★★★★ (Vercel AI SDK) | ★★★ (async-openai) | medium |
| 3. Type safety / TyDD | ★★★★ | ★★★★★ | ★★★★★ | high |
| 4. Iteration speed | ★★★★★ | ★★★★ | ★★ (compile) | high |
| 5. AI-assist fidelity | ★★★★★ | ★★★★★ | ★★★★ | medium |
| 6. Mercury port translation cost | ★★★★ | ★★★★★ (line-by-line) | ★★★ | medium |
| 7. Subscription-credential rewrite cost | **0** (done) | 1-2 weeks | 1-2 weeks | **decisive** |
| 8. P2 backtest viability | ★★★★★ | ★★ | ★★★ | **decisive** |
| 9. P4+ live-trading performance | ★★★ (numba / Rust ext) | ★★ | ★★★★★ | low (P1) |

## Alternatives considered and rejected

### TypeScript

**Pros**: Mercury patterns can be ported line-by-line; Vercel AI SDK
provides built-in tool-use plumbing; ecosystem and tooling polished.

**Rejection reason**: P2 backtest stack absence is fatal. A TS port
would either re-implement `pandas` + `statsmodels` + `vectorbt`
equivalents (multi-month effort) or shell out to Python (negates the
language switch). The line-by-line Mercury port advantage is real but
secondary to the P2 viability problem.

### Rust

**Pros**: Maximal type safety; native performance; future-proof for
P4 live-trading hot paths; `polars` is strong.

**Rejection reason**: Compile-time-driven friction on async research
code; statistical libraries nascent; 4-6 weeks of rewrite blocks
P1.2 progress. Rust's natural fit is HFT / latency-critical paths,
not research-prototype iteration.

### Python + Rust hybrid (PyO3)

**Pros**: Best of both — Python orchestration, Rust hot paths.

**Rejection reason**: Premature. CAQRS has no measured performance
bottleneck. Adding Rust modules pre-emptively introduces operational
complexity (cross-compilation, wheel building) without justification.
Reconsider once a backtest engine becomes a measured bottleneck —
likely mid-to-late P2.

### Julia

**Pros**: Closest single-language competitor for scientific
computing.

**Rejection reason**: Smaller ecosystem (no `instructor`-equivalent
for structured LLM output, weaker static-typing posture); higher
risk of bus-factor on domain-specific libraries; AI-assist quality
lower than Python or TypeScript.

### Go

**Pros**: Operational simplicity (single static binary); excellent
async story.

**Rejection reason**: Quant research ecosystem is minimal;
expressiveness of generics insufficient for the typed-agent /
typed-artifact pattern; AI-assist quality acceptable but lower for
domain modelling.

## Consequences

### Positive

- All seven prior PRs remain in force; no rewrite penalty.
- Mercury survey's 44 patterns translate to Python with no
  architectural compromises.
- `pandas` / `vectorbt` / `statsmodels` / `scipy` available from P2.
- `pydantic v2` strict + `mypy` strict + property tests give
  near-TypeScript type safety.

### Negative

- Type safety is **almost** TypeScript-level, not exceeding it. Some
  classes of error (variance, exhaustive matching) require runtime
  validation rather than compile-time rejection.
- Async story is `asyncio` — well-understood but not as ergonomic as
  Rust's `tokio` or TypeScript's native promises.
- Native dependency complexity (`better-sqlite3`-class issues) is
  largely avoided because `sqlite3` is in Python's stdlib, but any
  future native deps (e.g., `numba` AOT modules) reintroduce the
  problem.

### Risks (and mitigations)

| Risk | Mitigation |
| --- | --- |
| Runtime type errors that strict mypy missed | Hypothesis property tests + `extra="forbid"` + frozen models |
| Async bugs (cancellation, leaked tasks) | Single-threaded asyncio, strict `async with` discipline, `pytest-asyncio` integration tests |
| Backtest performance | Profile in P2; switch hot loop to numba or Rust ext if measured slow |
| Future contributor onboarding | Mypy strict + pydantic schemas keep contracts explicit; `docs/research/` audits design rationale |

## Reconsider when

- **Backtest engine is a measured bottleneck** (P2 mid-to-late). Add
  Rust modules via PyO3 for the hot path; main orchestrator stays
  Python.
- **Live trading latency requirements** appear (P4+). May justify a
  Rust execution-side module driven by Python research output.
- **Team scales beyond 3 contributors** with a TypeScript-skewed
  background. Re-evaluate; the benefit is contributor velocity, not
  type safety per se.
- **Python AI ecosystem stagnates** while a competing language
  (Rust, TS) overtakes the LLM-tooling story by a wide margin. Not
  observed at decision time.

## Implementation guidance for P1.2 onward

The 44 Mercury patterns from the survey are the canonical work list.
Each pattern has a CAQRS module target named in the survey's file 99.
Implementation order:

1. **P1.2** — agent layer foundation (loop_detector, preflight,
   state_machine, tool_registry, guardrails, role_template,
   headless_mode).
2. **P1.3** — memory layer (short_term, episodic, long_term,
   second_brain with merge / conflict / decay / consolidate / extract).
3. **P1.4** — orchestrator wiring (queue, budget, heartbeat,
   scheduler, event_log, introspection, self_fire).
4. **P3** — policy gateway (universe, size, leverage, concentration,
   approval_handler, temp_scope, elevation).

Type discipline:

- Every public function has full `mypy --strict` annotations.
- Every persistent record is a `pydantic` `BaseModel` with
  `frozen=True`, `extra="forbid"`, `strict=True`.
- Every cross-module boundary uses pydantic schemas, not dicts.
- Hypothesis property tests for any algorithm with non-trivial
  invariants (loop detector, memory merge, scoring).

This ADR is the last language decision until one of the
"reconsider when" triggers fires.
