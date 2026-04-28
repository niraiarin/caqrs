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
boundary. P2 ships the walk-forward backtest engine + J-Quants strategy library. P3
projects each `StrategyDecision` through a Policy Gateway (notional / ticker / loss-budget
constraints) into a `FeasibleAction`; the paper broker (P3.d, pending) consumes that
action without touching real funds. Live broker adapters land in P4 behind an explicit
human-approval workflow.

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

When GitHub Actions is unavailable (billing limit, outage), reproduce the upstream
matrix locally via `scripts/ci_local.sh`:

```bash
scripts/ci_local.sh             # active Python only (~2 s on a warm cache)
scripts/ci_local.sh --matrix    # Python 3.12 + 3.13 (uv auto-installs)
```

The script runs the same four gates (`ruff format --check`, `ruff check`, `mypy`,
`pytest --cov`) with `HYPOTHESIS_PROFILE=ci`. Paste its tail into the PR
description as evidence when CI itself can't run, then `gh pr merge`.

### Configuring API keys

Live tests and the standalone smoke scripts read secrets from the environment.
Copy `env.example` to `.env` (the latter is gitignored) and fill in real values:

```bash
cp env.example .env
$EDITOR .env
```

To auto-load `.env` on every command, use [dotenvx](https://dotenvx.com):

```bash
dotenvx run -- uv run pytest tests/live/ -v
dotenvx run -- uv run python scripts/live_smoke_jquants.py
```

`env.example` lists every variable the project reads (`CAQRS_LIVE`,
`CAQRS_LITELLM_*`, `JQUANTS_API_KEY`, `HYPOTHESIS_PROFILE`) with one-line
descriptions. Polymarket's CLOB / Gamma / archive endpoints are public and
need no key.

### Try a live data source

**Polymarket** implied probabilities are exposed via a public read-only API; no key
required:

```bash
uv run python scripts/live_smoke_polymarket.py
uv run python scripts/live_smoke_polymarket.py --slug fed-cuts-2026
# Hourly orderbook archive for backtesting (large parquet downloads):
uv run python scripts/live_smoke_polymarket_archive.py
```

**J-Quants** (JPX-official Japan equities) needs a free-tier API key — sign up at
[jpx-jquants.com](https://jpx-jquants.com/) and put it in `.env`:

```bash
dotenvx run -- uv run python scripts/live_smoke_jquants.py
dotenvx run -- uv run python scripts/live_smoke_jquants_observer.py --code 13010 --code 72030
# Walk-forward backtest factories (P2):
dotenvx run -- uv run python scripts/live_smoke_jquants_buy_and_hold.py
dotenvx run -- uv run python scripts/live_smoke_jquants_momentum.py
dotenvx run -- uv run python scripts/live_smoke_jquants_mean_reversion.py
# Side-by-side dispatch over StrategySpec discriminated union:
dotenvx run -- uv run python scripts/live_smoke_jquants_strategy_registry.py
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
│                   # polymarket/         (CLOB + Gamma clients + signal helper)
│                   # polymarket_archive/ (hourly Parquet from archive.pmxt.dev)
│                   # jquants/            (JPX-official daily OHLCV + master + signal helper)
├── backtest/       # walk-forward engine (polars) + J-Quants executor factories
│                   # buy-and-hold / top-K momentum / bottom-K mean reversion
│                   # StrategySpec discriminated union + make_jquants_executor dispatcher
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
   `BacktestExecutor` runs it (P2 ships a polars-backed walk-forward engine plus three
   J-Quants strategy templates: buy-and-hold, top-K momentum, bottom-K mean reversion;
   templates are composed into a single dispatcher via `make_jquants_executor(spec=...)`),
   and the Auditor checks the report against the hypothesis's acceptance criteria.
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

## Drive cycles unattended

`Heartbeat` is a pure interval-based fire tracker that composes with `CycleQueue` so a
caller's event loop can dispatch cycles on a schedule without threads or cron:

```python
import asyncio
from datetime import timedelta

from caqrs.orchestrator import CycleQueue, Heartbeat

heartbeat = Heartbeat(interval=timedelta(hours=4))
queue = CycleQueue(runner=runner)  # CycleRunner built once with all 6 agents

async def loop() -> None:
    while True:
        if heartbeat.is_due():
            queue.enqueue(build_observer_input())
            heartbeat.fire()
        await queue.run_one()
        await asyncio.sleep(1)
```

`CycleStore.prune_older_than(cutoff)` keeps the per-cycle archive bounded; call it
periodically (e.g. once per day) with a 30-day cutoff.

## Roadmap

| Phase     | Scope                                                          | Status                  |
| --------- | -------------------------------------------------------------- | ----------------------- |
| P0        | Artifact schemas + Agent protocol + CI                         | ✅                      |
| P1.1      | Provider stack (Anthropic / Codex / OpenAI-compat / registry)  | ✅                      |
| P1.2      | Six concrete agents on `LLMAgent` base                         | ✅                      |
| P1.3      | Memory: archive + auto-persist + episodic prune                | ✅ a + b + c            |
| P1.4      | Orchestrator (events, budget, runner, queue, heartbeat)        | ✅ a + b + c + d-mini + d-full |
| P1.5      | Live LLM smoke test (Observer)                                 | ✅                      |
| P1.6      | Polymarket data source + Observer / Hypothesis integration     | ✅ a–h (incl. archive)  |
| P1.7      | README refresh                                                 | ✅                      |
| P1.8      | Full-cycle live smoke test (LLM + Polymarket end-to-end)       | ✅                      |
| P1.11     | J-Quants data source + Observer integration                    | ✅ a + b                |
| P2        | Walk-forward backtest engine + J-Quants strategy library       | ✅ a–e                  |
| P3        | Policy Gateway + paper broker + asset/loss-limit projections   | ✅ a + b + c (d pending) |
| P4        | Live broker adapter (gated by human approval)                  | —                       |

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
