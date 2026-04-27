#!/usr/bin/env python3
"""Standalone smoke check for the J-Quants V2 client.

Reads the API key from ``JQUANTS_API_KEY`` (set up your free-tier
account at https://jpx-jquants.com/), fetches the listed-stock master
and the recent daily OHLCV for one ticker, and prints a summary.

Useful for confirming connectivity + schema assumptions before
opting into the gated pytest live suite.

Usage::

    export JQUANTS_API_KEY='...'
    uv run python scripts/live_smoke_jquants.py
    uv run python scripts/live_smoke_jquants.py --code 72030  # トヨタ
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from caqrs.data.jquants import JQuantsClient, JQuantsError

_KEY_VAR = "JQUANTS_API_KEY"


async def _run(code: str) -> int:
    api_key = os.environ.get(_KEY_VAR)
    if not api_key:
        print(
            f"[error] {_KEY_VAR} not set. Sign up at https://jpx-jquants.com/ "
            "(free tier) and export the key.",
            file=sys.stderr,
        )
        return 1

    try:
        async with JQuantsClient(api_key=api_key) as client:
            print(f"[fetch] /equities/master?code={code}")
            stocks = await client.list_master(code=code)
            print(f"[fetch] /equities/bars/daily?code={code}")
            bars = await client.daily_bars(code=code)
    except JQuantsError as exc:
        status = exc.status_code if exc.status_code is not None else "(no status)"
        print(f"[error] JQuantsError ({status}): {exc}", file=sys.stderr)
        return 1

    print()
    print("=== Listed-stock master ===")
    for s in stocks:
        print(
            f"  {s.code}  {s.company_name}  ({s.company_name_en or '-'})",
        )
        print(
            f"    sector: {s.sector_17_name or '-'} / {s.sector_33_name or '-'}",
        )
        print(
            f"    market: {s.market_name or '-'}  margin: {s.margin_name or '-'}",
        )
        print(f"    scale : {s.scale_category or '-'}")

    print()
    print("=== Daily bars (most recent 5) ===")
    most_recent = sorted(bars, key=lambda b: b.date)[-5:]
    for bar in most_recent:
        c = bar.close if bar.close is not None else "(none)"
        adj = bar.adjusted_close if bar.adjusted_close is not None else "(none)"
        vo = f"{bar.volume:,}" if bar.volume is not None else "(none)"
        print(f"  {bar.date.isoformat()}  close={c:<10} adj={adj:<10} vol={vo}")

    print()
    print(f"[summary] master rows={len(stocks)}  daily bars={len(bars)}")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--code",
        default="13010",  # 極洋 — small, stable, low-volume
        help="Ticker code (4 or 5 digits). Default: 13010 (極洋).",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    return asyncio.run(_run(args.code))


if __name__ == "__main__":
    sys.exit(main())
