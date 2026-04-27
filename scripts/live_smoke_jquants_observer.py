#!/usr/bin/env python3
"""Standalone smoke for fetch_jquants_asset_snapshot — JQuantsClient -> AssetSnapshot.

Reads the API key from ``JQUANTS_API_KEY`` (or via ``dotenvx run``),
fetches recent daily bars for one or more codes, and prints the
typed snapshot the Observer would receive.

Usage::

    dotenvx run -- uv run python scripts/live_smoke_jquants_observer.py
    uv run python scripts/live_smoke_jquants_observer.py --code 13010 --code 72030
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from caqrs.data.jquants import (
    JQuantsClient,
    JQuantsError,
    fetch_jquants_asset_snapshot,
)
from caqrs.schemas.observer import AssetSnapshot

_KEY_VAR = "JQUANTS_API_KEY"


def _format(snapshot: AssetSnapshot) -> str:
    last = f"{snapshot.last_close}" if snapshot.last_close is not None else "(none)"
    r1 = (
        f"{float(snapshot.return_1m) * 100:+.2f}%"
        if snapshot.return_1m is not None
        else "(insufficient history)"
    )
    r12 = (
        f"{float(snapshot.return_12m) * 100:+.2f}%"
        if snapshot.return_12m is not None
        else "(insufficient history)"
    )
    vol = (
        f"{float(snapshot.volatility_30d) * 100:.3f}%"
        if snapshot.volatility_30d is not None
        else "(insufficient history)"
    )
    return (
        f"{snapshot.ticker}\n"
        f"  last_close   : {last}\n"
        f"  return_1m    : {r1}\n"
        f"  return_12m   : {r12}\n"
        f"  vol_30d (sd) : {vol}"
    )


async def _run(codes: list[str]) -> int:
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
            for code in codes:
                print(f"[fetch] code={code}")
                snapshot = await fetch_jquants_asset_snapshot(client=client, code=code)
                print(_format(snapshot))
                print()
    except (JQuantsError, ValueError) as exc:
        print(f"[error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--code",
        action="append",
        default=None,
        help="Ticker code (4 or 5 digits). Repeat for multiple. Default: 13010, 72030.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    codes = args.code if args.code else ["13010", "72030"]
    return asyncio.run(_run(codes))


if __name__ == "__main__":
    sys.exit(main())
