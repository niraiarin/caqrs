"""EDINET official: recent_filings observer-side helper.

The EDINET official endpoint serves raw XBRL / PDF, which would
require a full XBRL parser to convert into agent-facing financial
metrics — out of scope for this slice. The helper here stays at the
**filing-metadata** level: "did issuer X file anything between
date A and date B, and what kind of document was it?". That
already supports several useful Observer signals:

- 大量保有報告書 detection (a 5%+ shareholder change = direct equity
  action signal)
- 有価証券報告書 / 四半期報告書 timing (sets the calendar for
  fundamentals expectations)
- 公開買付届出書 (M&A event signal)

Heavyweight XBRL-driven helpers (revenue / EPS extraction etc.) are
deferred until a caller actually needs them.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx

from caqrs.data.edinet.client import EdinetClient
from caqrs.data.edinet.observer_signals import fetch_recent_filings
from caqrs.data.edinet.schemas import EdinetDocument

_BASE = "https://api.edinet-fsa.go.jp/api/v2"


def _doc_record(
    *,
    seq: int = 1,
    doc_id: str = "S1000001",
    edinet_code: str = "E10001",
    sec_code: str | None = "13010",
    doc_type_code: str = "120",
) -> dict[str, object]:
    return {
        "seqNumber": seq,
        "docID": doc_id,
        "edinetCode": edinet_code,
        "secCode": sec_code,
        "JCN": "1234567890123",
        "filerName": "Test Co",
        "fundCode": None,
        "ordinanceCode": "010",
        "formCode": "030000",
        "docTypeCode": doc_type_code,
        "periodStart": "2024-01-01",
        "periodEnd": "2024-03-31",
        "submitDateTime": "2024-04-15 08:30",
        "docDescription": "Test filing",
        "issuerEdinetCode": None,
        "subjectEdinetCode": None,
        "subsidiaryEdinetCode": None,
        "currentReportReason": None,
        "parentDocID": None,
        "opeDateTime": None,
        "withdrawalStatus": "0",
        "docInfoEditStatus": "0",
        "disclosureStatus": "0",
        "xbrlFlag": "1",
        "pdfFlag": "1",
        "attachDocFlag": "0",
        "englishDocFlag": "0",
        "csvFlag": "1",
        "legalStatus": "1",
    }


def _payload(*, results: list[dict[str, object]]) -> dict[str, object]:
    return {
        "metadata": {
            "title": "提出された書類を一覧で取得",
            "parameter": {"date": "2026-04-28", "type": "2"},
            "resultset": {"count": len(results)},
            "processDateTime": "2026-04-28 09:00",
            "status": "200",
            "message": "OK",
        },
        "results": results,
    }


# === Single-day fetch ===


@pytest.mark.asyncio
@respx.mock
async def test_fetch_recent_filings_single_day_no_filter() -> None:
    """No edinet_codes filter → returns every document submitted on
    the requested day."""
    payload = _payload(
        results=[
            _doc_record(seq=1, edinet_code="E10001"),
            _doc_record(seq=2, edinet_code="E20002"),
            _doc_record(seq=3, edinet_code="E30003"),
        ],
    )
    respx.get(f"{_BASE}/documents.json").mock(
        return_value=httpx.Response(200, json=payload),
    )
    async with EdinetClient(api_key="k", throttle_seconds=0) as client:
        docs = await fetch_recent_filings(
            client=client,
            from_date=date(2026, 4, 28),
            to_date=date(2026, 4, 28),
        )
    assert len(docs) == 3
    assert all(isinstance(d, EdinetDocument) for d in docs)


@pytest.mark.asyncio
@respx.mock
async def test_fetch_recent_filings_filters_by_edinet_codes() -> None:
    payload = _payload(
        results=[
            _doc_record(seq=1, edinet_code="E10001"),
            _doc_record(seq=2, edinet_code="E20002"),
            _doc_record(seq=3, edinet_code="E30003"),
        ],
    )
    respx.get(f"{_BASE}/documents.json").mock(
        return_value=httpx.Response(200, json=payload),
    )
    async with EdinetClient(api_key="k", throttle_seconds=0) as client:
        docs = await fetch_recent_filings(
            client=client,
            from_date=date(2026, 4, 28),
            to_date=date(2026, 4, 28),
            edinet_codes=("E10001", "E30003"),
        )
    assert len(docs) == 2
    assert {d.edinet_code for d in docs} == {"E10001", "E30003"}


@pytest.mark.asyncio
@respx.mock
async def test_fetch_recent_filings_filters_by_doc_type_codes() -> None:
    payload = _payload(
        results=[
            _doc_record(seq=1, doc_type_code="120"),  # 四半期報告書
            _doc_record(seq=2, doc_type_code="030"),  # 有価証券届出書
            _doc_record(seq=3, doc_type_code="120"),
        ],
    )
    respx.get(f"{_BASE}/documents.json").mock(
        return_value=httpx.Response(200, json=payload),
    )
    async with EdinetClient(api_key="k", throttle_seconds=0) as client:
        docs = await fetch_recent_filings(
            client=client,
            from_date=date(2026, 4, 28),
            to_date=date(2026, 4, 28),
            doc_type_codes=("120",),
        )
    assert len(docs) == 2
    assert all(d.doc_type_code == "120" for d in docs)


# === Multi-day range ===


@pytest.mark.asyncio
@respx.mock
async def test_fetch_recent_filings_walks_date_range() -> None:
    """A 3-day range issues 3 calls and concatenates results."""
    route = respx.get(f"{_BASE}/documents.json").mock(
        side_effect=[
            httpx.Response(200, json=_payload(results=[_doc_record(seq=1)])),
            httpx.Response(200, json=_payload(results=[_doc_record(seq=2)])),
            httpx.Response(200, json=_payload(results=[_doc_record(seq=3)])),
        ],
    )
    async with EdinetClient(api_key="k", throttle_seconds=0) as client:
        docs = await fetch_recent_filings(
            client=client,
            from_date=date(2026, 4, 26),
            to_date=date(2026, 4, 28),
        )
    assert route.call_count == 3
    assert len(docs) == 3


@pytest.mark.asyncio
async def test_fetch_recent_filings_rejects_inverted_range() -> None:
    """from_date > to_date is a caller bug. Raise loudly rather than
    silently return empty."""
    async with EdinetClient(api_key="k", throttle_seconds=0) as client:
        with pytest.raises(ValueError, match="from_date"):
            await fetch_recent_filings(
                client=client,
                from_date=date(2026, 4, 28),
                to_date=date(2026, 4, 27),
            )


# === Empty days ===


@pytest.mark.asyncio
@respx.mock
async def test_fetch_recent_filings_handles_empty_days() -> None:
    """Weekends / holidays return resultset.count=0 — that's not an
    error, the helper just returns no docs for those days."""
    respx.get(f"{_BASE}/documents.json").mock(
        return_value=httpx.Response(200, json=_payload(results=[])),
    )
    async with EdinetClient(api_key="k", throttle_seconds=0) as client:
        docs = await fetch_recent_filings(
            client=client,
            from_date=date(2026, 4, 26),
            to_date=date(2026, 4, 27),
        )
    assert docs == ()
