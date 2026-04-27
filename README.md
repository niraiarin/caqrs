# CAQRS — Constrained Agentic Quant Research System

> **Status: private prototype.** Open-source release is planned but the repository is currently
> private while artifact schemas and the Policy Gateway stabilize.

CAQRS is an LLM-based **research orchestrator** for quantitative finance. It treats trading
agents not as profit-seekers but as **autonomous research systems under bounded execution
constraints**. The core loop is:

```
Observe → Hypothesize → Skeptic → Research → (Backtest) → Audit → Decide → (Policy Gateway) → Memory
```

P0–P1 close the *research* loop only — execution is suppressed at the `StrategyDecision`
boundary. Broker adapters arrive in P3 behind an explicit human-approval workflow.

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
uv run pytest                 # run tests (live tests skipped without CAQRS_LIVE=1)
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

### Try a live data source

Polymarket implied probabilities are exposed via a public read-only API; no key required.
The standalone smoke script discovers an active high-liquidity market and prints a
typed snapshot:

```bash
uv run python scripts/live_smoke_polymarket.py
# or pin to a specific market:
uv run python scripts/live_smoke_polymarket.py --slug fed-cuts-2026
```

For the full LLM pipeline against a LiteLLM gateway, see `scripts/live_smoke_observer.py`.

## Project layout

```
src/caqrs/
├── schemas/        # frozen, extra=forbid, mypy-strict pydantic artifacts
│                   # observer, hypothesis_card, skeptic, research_plan,
│                   # backtest_report, audit, decision, common
├── agents/         # Agent[I, O] protocol + LLMAgent base + 6 concrete agents
│                   # observer, hypothesis, skeptic, research, auditor, decider
│                   # plus prompts/ (guardrails, role template, polymarket block)
├── orchestrator/   # state machine, loop detector, preflight scanners,
│                   # event log, cycle budget, cycle runner, cycle queue
├── memory/         # episodic CycleStore (per-cycle archive + rolling index)
├── providers/      # Anthropic CLI, Codex CLI, OpenAI-compatible, registry,
│                   # subscription credential loaders (OpenClaw-derived)
├── data/           # external read-only data sources for the Observer
│                   # polymarket/ (CLOB + Gamma clients + Observer signal helper)
└── py.typed        # PEP 561 marker

tests/              # pytest + hypothesis; respx-mocked HTTP; live/ gated by CAQRS_LIVE=1
docs/               # ARCHITECTURE.md, lineage.md, decisions/ (ADRs),
                    # research/mercury-survey/ (44 named patterns)
scripts/            # standalone runnables (live smoke, manual integration checks)
```

## How a cycle runs

1. Caller builds an `ObserverInput` (universe, horizon, requested data dimensions,
   optional `polymarket_signals` pre-fetched via `caqrs.data.polymarket.fetch_polymarket_signal`).
2. `CycleRunner` drives the typed pipeline: Observer → Hypothesis → Skeptic. If the skeptic
   verdict is `PROCEED`, Research generates a walk-forward plan, the injected
   `BacktestExecutor` runs it (P2 plumbs in vectorbt; P1 takes a stub), and the Auditor
   checks the report against the hypothesis's acceptance criteria.
3. On `audit.verdict == PASS` the Decider emits a typed `StrategyDecision` with policy
   envelopes (`notional_cap_usd`, `max_position_weight`, `daily_loss_limit_usd`).
4. Per-step events flow into an `EventLog`; a `BudgetGuard` enforces token + wallclock caps;
   on agent failure or budget breach the runner emits `CYCLE_ABORTED` and transitions to
   `ERROR`.
5. If a `CycleStore` is wired in, the result and per-cycle events are persisted under
   `cycles/<id>/{result.json,events.jsonl}` with a one-line summary appended to
   `index.jsonl`.

A `CycleQueue` provides a serial dispatcher with reentrancy guard so concurrent enqueues
are safe but cycles execute one at a time.

## Roadmap

| Phase     | Scope                                                          | Status        |
| --------- | -------------------------------------------------------------- | ------------- |
| P0        | Artifact schemas + Agent protocol + CI                         | ✅            |
| P1.1      | Provider stack (Anthropic / Codex / OpenAI-compat / registry)  | ✅            |
| P1.2      | Six concrete agents on `LLMAgent` base                         | ✅            |
| P1.3      | Memory: per-cycle archive + auto-persist via runner            | ✅ a + b      |
| P1.4      | Orchestrator (events, budget, runner, queue)                   | ✅ a + b + c + d-mini |
| P1.5      | Live LLM smoke test (Observer)                                 | ✅            |
| P1.6      | Polymarket data source + Observer / Hypothesis integration     | ✅            |
| P1.7      | README refresh (this PR)                                       | ✅            |
| P2        | Walk-forward backtest engine (vectorbt) replacing the stub     | —             |
| P3        | Policy Gateway + paper broker + asset/loss-limit projections   | —             |
| P4        | Live broker adapter (gated by human approval)                  | —             |

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
