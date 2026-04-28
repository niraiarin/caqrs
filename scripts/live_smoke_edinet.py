"""Manually-triggered live smoke for the EDINET official API.

Run with::

    dotenvx run -- uv run python scripts/live_smoke_edinet.py

Defaults to a 3-day window ending today (UTC), no issuer filter, so
the output shows every document submitted in that window. Add
``--codes E02144,E03006`` to filter by EDINET code or
``--doc-type 120`` to filter by document type.

EDINET official has no published per-second rate limit but the
client's built-in 0.1 s throttle keeps us conservative — a 3-day
window typically returns hundreds of documents and the run takes
under a minute.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, date, datetime, timedelta

from caqrs.data.edinet import EdinetClient, EdinetError, fetch_recent_filings


def _parse_codes(raw: str | None) -> tuple[str, ...] | None:
    if not raw:
        return None
    return tuple(s.strip() for s in raw.split(",") if s.strip())


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        type=int,
        default=3,
        help="Days back from today to scan (default: 3).",
    )
    parser.add_argument(
        "--codes",
        type=str,
        default=None,
        help="Comma-separated EDINET codes to filter by (e.g. E02144,E03006).",
    )
    parser.add_argument(
        "--doc-type",
        dest="doc_type",
        type=str,
        default=None,
        help=(
            "Comma-separated docTypeCode to filter by (120=四半期, "
            "030=有報, 080=大量保有, 170=訂正)."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max documents to print (default: 20).",
    )
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    today = datetime.now(UTC).date()
    from_date = today - timedelta(days=args.days)
    edinet_codes = _parse_codes(args.codes)
    doc_type_codes = _parse_codes(args.doc_type)

    print(f"[plan] window={from_date}..{today}")
    if edinet_codes:
        print(f"[plan] edinet_codes={list(edinet_codes)}")
    if doc_type_codes:
        print(f"[plan] doc_type_codes={list(doc_type_codes)}")

    try:
        async with EdinetClient.from_env() as client:
            print("[run] fetching documents_list per day...")
            docs = await fetch_recent_filings(
                client=client,
                from_date=from_date,
                to_date=today,
                edinet_codes=edinet_codes,
                doc_type_codes=doc_type_codes,
            )
    except EdinetError as exc:
        print(f"[error] {exc}")
        return 1

    print(f"[done] {len(docs)} documents")
    for doc in docs[: args.limit]:
        print(
            f"  {doc.submit_date_time.date()} {doc.doc_type_code} "
            f"{doc.edinet_code} {doc.filer_name[:30]}: {doc.doc_description}",
        )
    if len(docs) > args.limit:
        print(f"  ...and {len(docs) - args.limit} more.")
    return 0


def main() -> int:
    return asyncio.run(_run(_parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())


# Avoid unused-import lint when type-checking standalone.
_ = date
