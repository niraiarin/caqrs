#!/usr/bin/env python3
"""Standalone smoke for the J-Quants buy-and-hold backtest executor.

Builds a tiny ResearchPlan with one walk-forward fold, runs the
make_jquants_buy_and_hold_executor pipeline against the real J-Quants
API, and prints the resulting BacktestReport metrics.

This is the same path CycleRunner takes when wired with the real
executor — verifying it end-to-end here catches drift between the
mock-tested wiring and the live API in one shot.

Usage::

    dotenvx run -- uv run python scripts/live_smoke_jquants_backtest.py
    # custom universe (Japan codes, comma-separated):
    dotenvx run -- uv run python scripts/live_smoke_jquants_backtest.py --universe 13010,72030
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal

from caqrs.backtest import make_jquants_buy_and_hold_executor
from caqrs.data.jquants import JQuantsClient, JQuantsError
from caqrs.schemas.common import RunMetadata, new_run_id
from caqrs.schemas.research_plan import (
    DataFrequency,
    ResearchPlan,
    WalkForwardWindow,
)

_KEY_VAR = "JQUANTS_API_KEY"


def _build_plan(*, universe: tuple[str, ...]) -> ResearchPlan:
    """One walk-forward fold inside the J-Quants free-tier window
    (2024-02-02 to 2026-02-02). Use mid-2025 for both train and test
    so the request lands well inside the subscription period."""
    return ResearchPlan(
        metadata=RunMetadata(
            run_id=new_run_id(),
            parent_id=None,
            agent_name="research",
            model_id="caqrs.scripts.live_smoke_jquants_backtest",
            created_at=datetime.now(UTC),
            llm_cost_usd=Decimal(0),
            latency_ms=0,
            token_in=0,
            token_out=0,
        ),
        hypothesis_run_id=new_run_id(),
        universe=universe,
        frequency=DataFrequency.DAILY,
        walk_forward=(
            WalkForwardWindow(
                train_start=datetime(2025, 1, 1, tzinfo=UTC),
                train_end=datetime(2025, 5, 31, tzinfo=UTC),
                test_start=datetime(2025, 6, 1, tzinfo=UTC),
                test_end=datetime(2025, 7, 31, tzinfo=UTC),
            ),
        ),
        cost_model_bps=Decimal("5"),  # 0.5 bp per side, ~ realistic for liquid Japanese names
        slippage_bps=Decimal("5"),
        seed=1,
    )


async def _run(universe: tuple[str, ...]) -> int:
    api_key = os.environ.get(_KEY_VAR)
    if not api_key:
        print(
            f"[error] {_KEY_VAR} not set. Sign up at https://jpx-jquants.com/ "
            "(free tier) and export the key.",
            file=sys.stderr,
        )
        return 1

    plan = _build_plan(universe=universe)
    fold_window = plan.walk_forward[0]
    print(
        f"[plan] universe={list(universe)} "
        f"fold={fold_window.test_start.date()}..{fold_window.test_end.date()}",
    )

    try:
        async with JQuantsClient(api_key=api_key) as client:
            executor = make_jquants_buy_and_hold_executor(client=client)
            report = await executor(plan)
    except (JQuantsError, ValueError) as exc:
        print(f"[error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print()
    print("=== Per-fold metrics ===")
    for fold in report.folds:
        print(
            f"  fold {fold.fold_index} "
            f"[{fold.test_start.date()}..{fold.test_end.date()}] "
            f"sharpe={float(fold.sharpe):+.3f}  "
            f"max_dd={float(fold.max_drawdown_pct):.2f}%  "
            f"turnover={float(fold.turnover):.4f}  "
            f"trades={fold.n_trades}  "
            f"pnl=${float(fold.pnl_usd):+,.0f}",
        )

    print()
    print("=== Aggregate ===")
    agg = report.aggregate
    print(
        f"  median_sharpe={float(agg.median_sharpe):+.3f}  "
        f"mean_sharpe={float(agg.mean_sharpe):+.3f}  "
        f"worst_sharpe={float(agg.worst_fold_sharpe):+.3f}",
    )
    print(
        f"  median_drawdown={float(agg.median_max_drawdown_pct):.2f}%  "
        f"total_pnl=${float(agg.total_pnl_usd):+,.0f}  "
        f"total_trades={agg.total_trades}",
    )
    return 0


def _parse_universe(value: str) -> tuple[str, ...]:
    return tuple(s.strip() for s in value.split(",") if s.strip())


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--universe",
        type=_parse_universe,
        default=("13010", "72030"),
        help="Comma-separated J-Quants ticker codes. Default: 13010,72030.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    return asyncio.run(_run(args.universe))


if __name__ == "__main__":
    sys.exit(main())
