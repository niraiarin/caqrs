#!/usr/bin/env python3
"""Standalone smoke for the J-Quants bottom-K mean-reversion executor.

Builds a tiny ResearchPlan with one walk-forward fold and runs the
make_jquants_mean_reversion_executor pipeline against the real
J-Quants API. Compares against the buy-and-hold baseline so the
strategy tilt is visible.

Usage::

    dotenvx run -- uv run python scripts/live_smoke_jquants_mean_reversion.py
    dotenvx run -- uv run python scripts/live_smoke_jquants_mean_reversion.py \
        --universe 13010,72030 --lookback 10 --bottom-k 1
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
    make_jquants_mean_reversion_executor,
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
            model_id="caqrs.scripts.live_smoke_jquants_mean_reversion",
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


async def _run(universe: tuple[str, ...], lookback: int, bottom_k: int) -> int:
    api_key = os.environ.get(_KEY_VAR)
    if not api_key:
        print(f"[error] {_KEY_VAR} not set.", file=sys.stderr)
        return 1

    plan = _build_plan(universe=universe)
    fold_window = plan.walk_forward[0]
    print(
        f"[plan] universe={list(universe)} "
        f"fold={fold_window.test_start.date()}..{fold_window.test_end.date()}",
    )
    print(f"[strategy] mean-reversion lookback={lookback} bottom_k={bottom_k}")

    try:
        async with JQuantsClient(api_key=api_key) as client:
            baseline_executor = make_jquants_buy_and_hold_executor(client=client)
            mean_rev_executor = make_jquants_mean_reversion_executor(
                client=client,
                lookback_days=lookback,
                bottom_k=bottom_k,
            )
            print()
            print("[run] buy-and-hold baseline...")
            baseline = await baseline_executor(plan)
            print()
            print(f"[run] mean-reversion (lookback={lookback}, bottom_k={bottom_k})...")
            mean_rev = await mean_rev_executor(plan)
    except (JQuantsError, ValueError) as exc:
        print(f"[error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print()
    _print_report("Buy-and-hold baseline", baseline)
    print()
    _print_report(
        f"Mean-reversion (lookback={lookback}, bottom_k={bottom_k})",
        mean_rev,
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
    parser.add_argument(
        "--lookback",
        type=int,
        default=10,
        help="Lookback in trading days for ranking. Default: 10.",
    )
    parser.add_argument(
        "--bottom-k",
        type=int,
        default=1,
        help="Number of bottom-momentum tickers to long. Default: 1.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    return asyncio.run(_run(args.universe, args.lookback, args.bottom_k))


if __name__ == "__main__":
    sys.exit(main())
