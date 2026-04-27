#!/usr/bin/env python3
"""Standalone smoke check for the Polymarket integration.

Discovers an active Polymarket market via the Gamma API, fetches the
composed Observer-ready signal (Gamma metadata + per-token CLOB
midpoint / spread / last-trade), and prints a human-readable summary.

No LLM endpoint, no API key. Exits 0 on success, 1 on any error.

Usage::

    uv run python scripts/live_smoke_polymarket.py
    uv run python scripts/live_smoke_polymarket.py --slug fed-cuts-2026

When ``--slug`` is omitted the script picks the first active market
returned by ``list_markets`` whose ``clob_token_ids`` are populated.
This is fine for a smoke check; production use should pass an
explicit slug or condition id.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from caqrs.data.polymarket import (
    PolymarketClobClient,
    PolymarketError,
    PolymarketGammaClient,
    fetch_polymarket_signal,
)
from caqrs.schemas.observer import PolymarketSignal


def _format_signal(signal: PolymarketSignal) -> str:
    lines: list[str] = [
        f"market_id : {signal.market_id}",
        f"slug      : {signal.slug or '(none)'}",
        f"question  : {signal.question or '(none)'}",
        f"end_date  : {signal.end_date.isoformat() if signal.end_date else '(none)'}",
        f"is_binary : {signal.is_binary}",
        f"fetched_at: {signal.fetched_at.isoformat()}",
        "outcomes:",
    ]
    for o in signal.outcomes:
        mid = f"{o.midpoint:.3f}" if o.midpoint is not None else "?"
        last = f"{o.last_trade_price:.3f}" if o.last_trade_price is not None else "?"
        spread = f"{o.spread:.3f}" if o.spread is not None else "?"
        lines.append(
            f"  - label={o.label!r:>10}  token={o.token_id:<14}  "
            f"midpoint={mid}  last_trade={last}  spread={spread}",
        )
    return "\n".join(lines)


async def _resolve_slug(gamma: PolymarketGammaClient) -> str:
    markets = await gamma.list_markets(active=True, closed=False, limit=20)
    for m in markets:
        if m.slug and m.clob_token_ids:
            return m.slug
    msg = "No active Polymarket markets with both slug and clob_token_ids found"
    raise RuntimeError(msg)


async def _run(slug: str | None) -> int:
    async with PolymarketGammaClient() as gamma, PolymarketClobClient() as clob:
        if slug is None:
            print("[discover] Listing active markets to pick one with populated tokens...")
            slug = await _resolve_slug(gamma)
            print(f"[discover] Using slug: {slug}")

        try:
            signal = await fetch_polymarket_signal(
                gamma_client=gamma,
                clob_client=clob,
                identifier=slug,
            )
        except PolymarketError as exc:
            status = exc.status_code if exc.status_code is not None else "(no status)"
            print(f"[error] PolymarketError ({status}): {exc}", file=sys.stderr)
            return 1

    print()
    print(_format_signal(signal))
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--slug",
        default=None,
        help="Polymarket market slug or numeric id; auto-discovers if omitted.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    return asyncio.run(_run(args.slug))


if __name__ == "__main__":
    sys.exit(main())
