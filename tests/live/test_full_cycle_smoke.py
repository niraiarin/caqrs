"""Live smoke test for the full CycleRunner pipeline.

Drives a real research cycle end-to-end: discover an active
Polymarket market, fetch its signal via the Observer helper, then
run all six LLM agents (Observer → Hypothesis → Skeptic → Research →
[stub backtest] → Auditor → [Decider when audit passes]) through
``CycleRunner`` against a real LiteLLM gateway.

Gated by ``CAQRS_LIVE=1`` + ``CAQRS_LITELLM_MODEL``. **Costs real
tokens on every run** — six LLM calls per cycle in the happy path.
The test asserts only that the cycle completes without an
``aborted_reason``; any of the legitimate terminal states are
accepted because LLM output drives the routing (a Skeptic ``KILL``
ends at ``SCRUTINIZING``, a failing audit ends at ``AUDITING``,
a passing audit ends at ``DECIDING``).
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from caqrs.agents import (
    AuditorAgent,
    DeciderAgent,
    HypothesisAgent,
    ObserverAgent,
    ResearchAgent,
    SkepticAgent,
)
from caqrs.data.polymarket import (
    PolymarketClobClient,
    PolymarketGammaClient,
    fetch_polymarket_signal,
)
from caqrs.orchestrator import (
    CycleBudget,
    CycleResult,
    CycleRunner,
    EventLog,
    OrchestratorState,
    new_cycle_id,
)
from caqrs.providers import OpenAICompatProvider
from caqrs.schemas.backtest_report import (
    AggregateMetrics,
    BacktestReport,
    FoldMetrics,
)
from caqrs.schemas.common import RunMetadata, new_run_id
from caqrs.schemas.observer import DataDimension, ObserverInput
from caqrs.schemas.research_plan import ResearchPlan


def _stub_backtest_metadata() -> RunMetadata:
    return RunMetadata(
        run_id=new_run_id(),
        parent_id=None,
        agent_name="backtest_stub",
        model_id="stub",
        created_at=datetime.now(UTC),
        llm_cost_usd=Decimal(0),
        latency_ms=0,
        token_in=0,
        token_out=0,
    )


async def _stub_backtest(plan: ResearchPlan) -> BacktestReport:
    """Synthetic backtest report with a single fold matching the plan's first window.

    The Auditor inspects the report against the hypothesis's
    acceptance criteria; the LLM-emitted criteria pin the metric_path
    that the audit evaluates. To keep the smoke test deterministic
    (no second-order LLM-influenced flakiness) we emit moderately
    favourable numbers — a Sharpe of 1.0, 8% drawdown — that should
    pass typical hypothesis bands but isn't extreme enough to look
    fabricated.
    """
    first = plan.walk_forward[0]
    return BacktestReport(
        metadata=_stub_backtest_metadata(),
        plan_run_id=plan.metadata.run_id,
        folds=(
            FoldMetrics(
                fold_index=0,
                test_start=first.test_start,
                test_end=first.test_end,
                sharpe=Decimal("1.0"),
                max_drawdown_pct=Decimal("8"),
                turnover=Decimal("2.0"),
                n_trades=120,
                pnl_usd=Decimal("12345"),
            ),
        ),
        aggregate=AggregateMetrics(
            median_sharpe=Decimal("1.0"),
            mean_sharpe=Decimal("1.0"),
            worst_fold_sharpe=Decimal("1.0"),
            median_max_drawdown_pct=Decimal("8"),
            total_pnl_usd=Decimal("12345"),
            total_trades=120,
        ),
    )


_VALID_TERMINAL_STATES = {
    # Audit passed → Decider ran
    OrchestratorState.DECIDING,
    # Audit failed → cycle ended at auditor
    OrchestratorState.AUDITING,
    # Skeptic killed or required revision → cycle ended at skeptic
    OrchestratorState.SCRUTINIZING,
}


@pytest.mark.live
@pytest.mark.asyncio
async def test_full_cycle_runs_against_litellm_with_polymarket_signal(
    litellm_provider: OpenAICompatProvider,
) -> None:
    # --- 1. Discover a Polymarket market and fetch a signal ------------
    async with PolymarketGammaClient() as gamma:
        markets = await gamma.list_markets(active=True, closed=False, limit=20)
    market = next((m for m in markets if m.slug and m.clob_token_ids), None)
    if market is None:
        pytest.fail(
            "No active Polymarket markets with both slug and clob_token_ids returned.",
        )

    async with PolymarketGammaClient() as gamma, PolymarketClobClient() as clob:
        assert market.slug is not None
        signal = await fetch_polymarket_signal(
            gamma_client=gamma,
            clob_client=clob,
            identifier=market.slug,
        )

    # --- 2. Build the cycle runner with all six real agents ------------
    runner = CycleRunner(
        observer=ObserverAgent(provider=litellm_provider, max_output_tokens=2048),
        hypothesis=HypothesisAgent(provider=litellm_provider, max_output_tokens=2048),
        skeptic=SkepticAgent(provider=litellm_provider, max_output_tokens=2048),
        research=ResearchAgent(provider=litellm_provider, max_output_tokens=2048),
        auditor=AuditorAgent(provider=litellm_provider, max_output_tokens=2048),
        decider=DeciderAgent(provider=litellm_provider, max_output_tokens=2048),
        backtest_executor=_stub_backtest,
        event_log=EventLog(),
        budget=CycleBudget(
            cycle_id=new_cycle_id(),
            token_cap=200_000,
            wallclock_seconds_cap=600.0,
        ),
    )

    # --- 3. Run the cycle ----------------------------------------------
    observer_input = ObserverInput(
        universe=("AAPL", "MSFT", "SPY"),
        as_of=datetime.now(UTC),
        horizon_days=30,
        dimensions=(DataDimension.PRICES, DataDimension.NEWS, DataDimension.MACRO),
        polymarket_signals=(signal,),
    )
    result = await runner.run(observer_input)

    # --- 4. Assert structural success ----------------------------------
    assert isinstance(result, CycleResult)
    assert result.aborted_reason is None, f"Cycle aborted: {result.aborted_reason}"
    assert result.terminal_state in _VALID_TERMINAL_STATES, (
        f"Unexpected terminal_state {result.terminal_state}; "
        f"expected one of {_VALID_TERMINAL_STATES}."
    )
    assert result.artifacts.observer is not None
    assert result.artifacts.hypothesis is not None
    assert result.artifacts.skeptic is not None
    # Token usage round-tripped from each provider call into the cycle totals
    assert result.total_token_in > 0
    assert result.total_token_out > 0
