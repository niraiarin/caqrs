#!/usr/bin/env python3
"""Standalone smoke for the J-Quants top-K momentum executor.

Builds a tiny ResearchPlan with one walk-forward fold and runs the
make_jquants_momentum_executor pipeline against the real J-Quants
API. Compares against the buy-and-hold baseline (top_k=N) so the
output makes the momentum tilt visible.

Usage::

    dotenvx run -- uv run python scripts/live_smoke_jquants_momentum.py
    dotenvx run -- uv run python scripts/live_smoke_jquants_momentum.py \
        --universe 13010,72030,67580 --lookback 21 --top-k 1
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal

from caqrs.backtest import (
    make_jquants_buy_and_hold_executor,
    make_jquants_momentum_executor,
)
from caqrs.data.jquants import JQuantsClient, JQuantsError
from caqrs.schemas.backtest_report import BacktestReport
from caqrs.schemas.common import RunMetadata, new_run_id
from caqrs.schemas.research_plan import (
    DataFrequency,
    ResearchPlan,
    WalkForwardWindow,
)

_KEY_VAR = "JQUANTS_API_KEY"


def _build_plan(*, universe: tuple[str, ...]) -> ResearchPlan:
    return ResearchPlan(
        metadata=RunMetadata(
            run_id=new_run_id(),
            parent_id=None,
            agent_name="research",
            model_id="caqrs.scripts.live_smoke_jquants_momentum",
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
        cost_model_bps=Decimal("5"),
        slippage_bps=Decimal("5"),
        seed=1,
    )


def _print_report(label: str, report: BacktestReport) -> None:
    print(f"=== {label} ===")
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
    agg = report.aggregate
    print(
        f"  aggregate sharpe(median/mean/worst)="
        f"{float(agg.median_sharpe):+.3f} / "
        f"{float(agg.mean_sharpe):+.3f} / "
        f"{float(agg.worst_fold_sharpe):+.3f}  "
        f"total_pnl=${float(agg.total_pnl_usd):+,.0f}  "
        f"total_trades={agg.total_trades}",
    )


async def _run(universe: tuple[str, ...], lookback: int, top_k: int) -> int:
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
    print(f"[strategy] momentum lookback={lookback} top_k={top_k}")

    try:
        async with JQuantsClient(api_key=api_key) as client:
            baseline_executor = make_jquants_buy_and_hold_executor(client=client)
            momentum_executor = make_jquants_momentum_executor(
                client=client,
                lookback_days=lookback,
                top_k=top_k,
            )
            print()
            print("[run] buy-and-hold baseline (top_k=N)...")
            baseline = await baseline_executor(plan)
            print()
            print(f"[run] momentum (lookback={lookback}, top_k={top_k})...")
            momentum = await momentum_executor(plan)
    except (JQuantsError, ValueError) as exc:
        print(f"[error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print()
    _print_report("Buy-and-hold baseline", baseline)
    print()
    _print_report(f"Momentum (lookback={lookback}, top_k={top_k})", momentum)
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
        default=("13010", "72030", "67580"),  # 極洋 / トヨタ / SoftBank
        help="Comma-separated J-Quants ticker codes. Default: 13010,72030,67580.",
    )
    parser.add_argument(
        "--lookback",
        type=int,
        default=21,
        help="Lookback in trading days for momentum ranking. Default: 21 (1 month).",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=1,
        help="Number of top-momentum tickers to long. Default: 1.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    return asyncio.run(_run(args.universe, args.lookback, args.top_k))


if __name__ == "__main__":
    sys.exit(main())
