"""Manually-triggered live smoke for EDINET DB.

⚠️  Free plan = 100 requests / day. This script costs roughly:
- 1 request for ``--companies-page`` (cached after first run)
- 1 request per ``--code`` for fundamentals (cached after first run)
- 1 request for ``--roe-limit`` (cached after first run)

Re-running on the same day with the same args costs **zero**
because every endpoint goes through the SQLite cache.

Run with::

    dotenvx run -- uv run python scripts/live_smoke_edinetdb.py
    dotenvx run -- uv run python scripts/live_smoke_edinetdb.py \\
        --code E02144,E03006 --roe-limit 5

Both the cache and the daily-quota tracker are wired in by default
so any production usage automatically respects the budget.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from caqrs.data.edinetdb import (
    DailyQuotaTracker,
    EdinetDbCache,
    EdinetDbClient,
    EdinetDbError,
    EdinetDbQuotaExhaustedError,
    fetch_edinetdb_company_fundamentals,
)


def _parse_codes(raw: str) -> tuple[str, ...]:
    return tuple(s.strip() for s in raw.split(",") if s.strip())


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--code",
        type=_parse_codes,
        default=("E02144",),  # Toyota
        help="Comma-separated EDINET codes to inspect (default: E02144 = Toyota).",
    )
    parser.add_argument(
        "--companies-page",
        type=int,
        default=1,
        help="Page number for the /companies preview (default: 1).",
    )
    parser.add_argument(
        "--companies-per-page",
        type=int,
        default=3,
        help="per_page for the /companies preview (default: 3).",
    )
    parser.add_argument(
        "--roe-limit",
        type=int,
        default=5,
        help="Top-N for /rankings/roe (default: 5).",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path.home() / ".caqrs",
        help="Directory for the cache + quota SQLite files.",
    )
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    cache = EdinetDbCache(db_path=args.cache_dir / "edinetdb-cache.db")
    quota = DailyQuotaTracker(db_path=args.cache_dir / "edinetdb-quota.db")

    print(f"[plan] cache_dir={args.cache_dir}")
    print(f"[plan] quota_remaining={quota.quota_remaining()}/100 (UTC day)")

    try:
        async with EdinetDbClient.from_env(cache=cache, quota_tracker=quota) as client:
            # 1. Companies preview
            print()
            print(
                f"[run] /companies page={args.companies_page} per_page={args.companies_per_page}",
            )
            companies = await client.list_companies(
                page=args.companies_page,
                per_page=args.companies_per_page,
            )
            assert companies.meta.pagination is not None
            print(
                f"[done] {len(companies.data)} companies (total={companies.meta.pagination.total})",
            )
            for c in companies.data:
                print(
                    f"  {c.edinet_code} {c.sec_code} {c.name_ja[:25]} "
                    f"({c.industry}) credit={c.credit_rating}/{c.credit_score}",
                )

            # 2. Per-code fundamentals
            for code in args.code:
                print()
                print(f"[run] fundamentals for {code}")
                snap = await fetch_edinetdb_company_fundamentals(
                    client=client,
                    edinet_code=code,
                )
                if snap.latest_fiscal_year is None:
                    print(f"  no fiscal-year rows available for {code}")
                    continue
                print(
                    f"  FY{snap.latest_fiscal_year} "
                    f"revenue={snap.latest_revenue} "
                    f"net_income={snap.latest_net_income} "
                    f"eps={snap.latest_eps}",
                )
                if snap.revenue_growth_yoy is not None:
                    print(
                        f"  Y/Y growth — revenue: {float(snap.revenue_growth_yoy):+.1%} "
                        f"net_income: "
                        f"{float(snap.net_income_growth_yoy or 0):+.1%}",
                    )

            # 3. ROE leaderboard
            print()
            print(f"[run] /rankings/roe limit={args.roe_limit}")
            roe = await client.ranking_roe(limit=args.roe_limit)
            for r in roe:
                print(
                    f"  #{r.rank} {r.name_ja[:25]} ({r.industry}) "
                    f"ROE={float(r.value):+.2f}{r.unit} FY{r.fiscal_year}",
                )

    except EdinetDbQuotaExhaustedError as exc:
        print(f"[stop] {exc}")
        return 1
    except EdinetDbError as exc:
        print(f"[error] {exc}")
        return 1

    print()
    print(f"[done] quota_remaining={quota.quota_remaining()}/100 (UTC day)")
    return 0


def main() -> int:
    return asyncio.run(_run(_parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
