"""Observer-side helpers over :class:`EdinetClient`.

The official EDINET endpoint returns raw XBRL / PDF bodies; turning
those into agent-facing financial metrics requires a full XBRL
parser, which is deliberately out of scope here. This module stays
at the **filing-metadata** level so callers can detect filing
events without parsing document bodies:

- 大量保有報告書 (5%+ shareholder change) — direct equity action
- 有価証券報告書 / 四半期報告書 timing — fundamentals expectations
- 公開買付届出書 — M&A event signal
- 訂正書類 — restatement risk

Quantitative readers (revenue / EPS extraction etc.) belong in a
separate module that wraps an XBRL parser; this helper avoids that
dependency tree for now.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date as _date
from datetime import timedelta

from caqrs.data.edinet.client import EdinetClient
from caqrs.data.edinet.schemas import EdinetDocument


async def fetch_recent_filings(
    *,
    client: EdinetClient,
    from_date: _date,
    to_date: _date,
    edinet_codes: Sequence[str] | None = None,
    doc_type_codes: Sequence[str] | None = None,
) -> tuple[EdinetDocument, ...]:
    """Concatenate the ``documents_list`` results for every calendar
    day in ``[from_date, to_date]`` (inclusive), optionally filtered
    by issuer EDINET code and document type code.

    Pagination on the EDINET side is per-day; this helper just walks
    the date range and applies in-memory filters. The default
    throttle on the underlying :class:`EdinetClient` (0.1 s, 10 req/s)
    keeps a multi-day fetch comfortably below the gateway's
    rate-limit window.

    Parameters
    ----------
    client:
        An open :class:`EdinetClient` (caller owns the lifecycle —
        this helper does not call ``aclose``).
    from_date / to_date:
        Inclusive date range. ``ValueError`` on inverted ranges.
    edinet_codes:
        When supplied, results are filtered to documents whose
        ``edinet_code`` is in this set. ``None`` means no filter.
    doc_type_codes:
        When supplied, results are filtered to documents whose
        ``doc_type_code`` is in this set. Common codes:

        - ``"120"`` 四半期報告書 / quarterly report
        - ``"160"`` 半期報告書 / half-year report
        - ``"030"`` 有価証券届出書 / securities registration
        - ``"080"`` 大量保有報告書 / large-shareholding report
        - ``"170"`` 訂正報告書 / amendment

    Returns
    -------
    tuple[EdinetDocument, ...]
        Documents matching all supplied filters, in the order EDINET
        returned them per day (i.e. by ``submitDateTime``).
    """
    if from_date > to_date:
        msg = f"from_date {from_date} must be <= to_date {to_date}; got an inverted range."
        raise ValueError(msg)

    code_filter = frozenset(edinet_codes) if edinet_codes else None
    type_filter = frozenset(doc_type_codes) if doc_type_codes else None

    out: list[EdinetDocument] = []
    cursor = from_date
    while cursor <= to_date:
        listing = await client.documents_list(date_=cursor, include_results=True)
        for doc in listing.results:
            if code_filter is not None and doc.edinet_code not in code_filter:
                continue
            if type_filter is not None and doc.doc_type_code not in type_filter:
                continue
            out.append(doc)
        cursor += timedelta(days=1)
    return tuple(out)
