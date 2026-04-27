# CAQRS — Constrained Agentic Quant Research System

> **Status: private prototype.** Open-source release is planned but the repository is currently
> private while artifact schemas and the Policy Gateway stabilize.

CAQRS is an LLM-based **research orchestrator** for quantitative finance. It treats trading
agents not as profit-seekers but as **autonomous research systems under bounded execution
constraints**. The core loop is:

```
Observe → Hypothesize → Skeptic → Research → Audit → Decide → (Policy Gateway) → Memory
```

P0 closes the *research* loop only — execution is suppressed at the `StrategyDecision`
boundary. Broker adapters arrive in P3 behind explicit human-approval workflow.

## Why a separate repo?

CAQRS imports several design ideas from [Mercury](https://github.com/) (permission gateway,
agentic loop transparency, second-brain memory) but is **not** a fork. The financial-research
stack (vectorbt, pandas, statsmodels, yfinance, FRED, PRAW) does not cross-compile from
TypeScript. See `docs/lineage.md` and `docs/decisions/0001-independent-repo.md`.

## Repository status

The repository is intentionally kept private until:

- Artifact schemas (`src/caqrs/schemas/`) stabilize at `schema_version=1`.
- Policy Gateway (P3) is implemented and verified by regression tests.
- Data-source ToS audit (`LICENSE_AND_TOS.md`) is complete.

The codebase is **written for OSS release** (Apache-2.0, public-style CI, OSS-grade docs).

## Quickstart

Requires Python ≥ 3.12 and [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync                       # install deps (creates .venv/)
uv run pytest                 # run tests
uv run ruff check .           # lint
uv run ruff format --check .  # format check
uv run mypy src tests         # strict type-check
```

If you have [`just`](https://github.com/casey/just) installed (`brew install just`):

```bash
just test
just lint
just typecheck
just ci          # run everything CI runs
```

## Project layout

```
src/caqrs/
├── schemas/         # pydantic artifact schemas (frozen, extra=forbid, mypy strict)
├── agents/          # Agent protocol; concrete agents land here in P1
└── py.typed         # PEP 561 marker
tests/               # pytest + hypothesis property-based tests
docs/                # ARCHITECTURE.md, lineage.md, decisions/ (ADRs)
```

## Roadmap

| Phase | Scope                                                       | Status |
| ----- | ----------------------------------------------------------- | ------ |
| P0    | Artifact schemas + Agent protocol + CI                      | ✅     |
| P1    | Observer + Hypothesis + Skeptic + Auditor (closed loop)     | —      |
| P2    | Research Agent + walk-forward backtest (vectorbt)           | —      |
| P3    | Policy Gateway + paper broker                               | —      |
| P4    | Live broker adapter (optional, gated by human approval)     | —      |

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
