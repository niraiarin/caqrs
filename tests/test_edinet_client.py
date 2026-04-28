"""EDINET v2 client behaviour.

The client follows the official spec (`ESE140206.pdf`):

- ``Subscription-Key`` is a **query parameter** on every request
  (not an HTTP header).
- HTTP layer always returns 200 from the upstream gateway. Errors
  are signalled via ``metadata.status`` ≠ "200" in the JSON body
  (or, for 401, a flat ``{StatusCode, message}`` shape).
- Response ``Content-Type`` distinguishes ZIP / PDF / error JSON
  for the document-download endpoint.
"""

from __future__ import annotations

import os
from datetime import date
from unittest.mock import patch

import httpx
import pytest
import respx

from caqrs.data.edinet.client import EdinetClient, EdinetError

_BASE = "https://api.edinet-fsa.go.jp/api/v2"


def _payload(
    *, count: int = 0, results: list[dict[str, object]] | None = None
) -> dict[str, object]:
    return {
        "metadata": {
            "title": "提出された書類を一覧で取得",
            "parameter": {"date": "2026-04-28", "type": "2"},
            "resultset": {"count": count},
            "processDateTime": "2026-04-28 09:00",
            "status": "200",
            "message": "OK",
        },
        "results": results or [],
    }


def _result(*, seq: int = 1, doc_id: str = "S1000001") -> dict[str, object]:
    return {
        "seqNumber": seq,
        "docID": doc_id,
        "edinetCode": "E10001",
        "secCode": "13010",
        "JCN": "6000012010023",
        "filerName": "極洋株式会社",
        "fundCode": None,
        "ordinanceCode": "010",
        "formCode": "030000",
        "docTypeCode": "120",
        "periodStart": "2024-01-01",
        "periodEnd": "2024-03-31",
        "submitDateTime": "2024-04-15 08:30",
        "docDescription": "第三四半期報告書",
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


# === documents_list happy path ===


@pytest.mark.asyncio
@respx.mock
async def test_documents_list_returns_typed_records() -> None:
    route = respx.get(f"{_BASE}/documents.json").mock(
        return_value=httpx.Response(
            200,
            json=_payload(count=2, results=[_result(seq=1), _result(seq=2, doc_id="S1000002")]),
        ),
    )

    async with EdinetClient(api_key="my-key") as client:
        listing = await client.documents_list(date_=date(2026, 4, 28))

    assert route.called
    params = route.calls.last.request.url.params
    # Subscription-Key is a query parameter per spec section 3-1-1.
    assert params["Subscription-Key"] == "my-key"
    # Default type=2 (full list + metadata).
    assert params["type"] == "2"
    assert params["date"] == "2026-04-28"

    assert listing.metadata.resultset is not None
    assert listing.metadata.resultset.count == 2
    assert len(listing.results) == 2
    assert listing.results[0].doc_id == "S1000001"
    assert listing.results[0].sec_code == "13010"


@pytest.mark.asyncio
@respx.mock
async def test_documents_list_metadata_only_uses_type_1() -> None:
    """type=1 returns metadata only — cheaper for "is anything new
    today" polls."""
    route = respx.get(f"{_BASE}/documents.json").mock(
        return_value=httpx.Response(200, json=_payload()),
    )
    async with EdinetClient(api_key="my-key") as client:
        await client.documents_list(date_=date(2026, 4, 28), include_results=False)

    assert route.calls.last.request.url.params["type"] == "1"


# === Auth ===


@pytest.mark.asyncio
async def test_client_requires_api_key_at_first_call() -> None:
    """A client constructed without a key fails loudly at first
    request (not at construction) so test fixtures can build a
    client and then patch the key."""
    async with EdinetClient(api_key=None) as client:
        with pytest.raises(EdinetError, match="api_key"):
            await client.documents_list(date_=date(2026, 4, 28))


def test_from_env_reads_edinet_api_key() -> None:
    with patch.dict(os.environ, {"EDINET_API_KEY": "env-supplied-key"}, clear=False):
        client = EdinetClient.from_env()
    assert client.api_key == "env-supplied-key"


def test_from_env_raises_when_missing() -> None:
    env = {k: v for k, v in os.environ.items() if k != "EDINET_API_KEY"}
    with (
        patch.dict(os.environ, env, clear=True),
        pytest.raises(EdinetError, match="EDINET_API_KEY"),
    ):
        EdinetClient.from_env()


# === Error handling: HTTP 200 + metadata.status != "200" ===


@pytest.mark.asyncio
@respx.mock
async def test_documents_list_raises_on_metadata_404() -> None:
    """EDINET wraps logical errors in HTTP 200 + metadata.status =
    "404" / "400" / "500". The client surfaces this as EdinetError so
    callers don't have to dig into the metadata."""
    respx.get(f"{_BASE}/documents.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "metadata": {
                    "title": "提出された書類を一覧で取得",
                    "status": "404",
                    "message": "Not Found",
                },
            },
        ),
    )
    async with EdinetClient(api_key="my-key") as client:
        with pytest.raises(EdinetError, match="404"):
            await client.documents_list(date_=date(2026, 4, 28))


@pytest.mark.asyncio
@respx.mock
async def test_documents_list_raises_on_metadata_400_with_message() -> None:
    respx.get(f"{_BASE}/documents.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "metadata": {
                    "title": "提出された書類を一覧で取得",
                    "status": "400",
                    "message": "Bad Request",
                },
            },
        ),
    )
    async with EdinetClient(api_key="my-key") as client:
        with pytest.raises(EdinetError, match="Bad Request"):
            await client.documents_list(date_=date(2026, 4, 28))


# === Error handling: 401 with flat shape ===


@pytest.mark.asyncio
@respx.mock
async def test_401_with_flat_shape_raises_edinet_error() -> None:
    """401 responses use a flat ``{StatusCode, message}`` payload per
    spec 3-3 example B (different from the metadata wrapper). The
    client recognises both shapes."""
    respx.get(f"{_BASE}/documents.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "StatusCode": 401,
                "message": (
                    "Access denied due to invalid subscription key. "
                    "Make sure to provide a valid key for an active subscription."
                ),
            },
        ),
    )
    async with EdinetClient(api_key="bad-key") as client:
        with pytest.raises(EdinetError, match="401"):
            await client.documents_list(date_=date(2026, 4, 28))


# === Throttle ===


@pytest.mark.asyncio
@respx.mock
async def test_throttle_zero_disables_pacing() -> None:
    """Tests pass throttle_seconds=0 explicitly to keep them fast.
    The default (0.1s) is reasonable for live usage."""
    respx.get(f"{_BASE}/documents.json").mock(
        return_value=httpx.Response(200, json=_payload()),
    )
    async with EdinetClient(api_key="k", throttle_seconds=0) as client:
        # 5 sequential calls at throttle=0 should complete instantly.
        for _ in range(5):
            await client.documents_list(date_=date(2026, 4, 28))


# === download_document ===


@pytest.mark.asyncio
@respx.mock
async def test_download_document_returns_zip_bytes_on_octet_stream() -> None:
    fake_zip = b"PK\x03\x04...ZIPCONTENT..."
    respx.get(f"{_BASE}/documents/S1000001").mock(
        return_value=httpx.Response(
            200,
            content=fake_zip,
            headers={"content-type": "application/octet-stream"},
        ),
    )
    async with EdinetClient(api_key="my-key") as client:
        body = await client.download_document(doc_id="S1000001", doc_type=1)
    assert body == fake_zip


@pytest.mark.asyncio
@respx.mock
async def test_download_document_returns_pdf_bytes_on_pdf_content_type() -> None:
    fake_pdf = b"%PDF-1.4\n..."
    respx.get(f"{_BASE}/documents/S1000001").mock(
        return_value=httpx.Response(
            200,
            content=fake_pdf,
            headers={"content-type": "application/pdf"},
        ),
    )
    async with EdinetClient(api_key="my-key") as client:
        body = await client.download_document(doc_id="S1000001", doc_type=2)
    assert body == fake_pdf


@pytest.mark.asyncio
@respx.mock
async def test_download_document_raises_when_content_type_is_json() -> None:
    """Per spec 3-2-2 / 3-3 callout: download endpoint returns JSON
    only on error. Detect via Content-Type and raise EdinetError."""
    respx.get(f"{_BASE}/documents/INVALID").mock(
        return_value=httpx.Response(
            200,
            json={
                "metadata": {
                    "title": "提出書類を取得",
                    "status": "404",
                    "message": "Not Found",
                },
            },
            headers={"content-type": "application/json; charset=utf-8"},
        ),
    )
    async with EdinetClient(api_key="my-key") as client:
        with pytest.raises(EdinetError, match="404"):
            await client.download_document(doc_id="INVALID", doc_type=1)


@pytest.mark.asyncio
@respx.mock
async def test_download_document_passes_type_param() -> None:
    route = respx.get(f"{_BASE}/documents/S1000001").mock(
        return_value=httpx.Response(
            200,
            content=b"",
            headers={"content-type": "application/pdf"},
        ),
    )
    async with EdinetClient(api_key="my-key") as client:
        await client.download_document(doc_id="S1000001", doc_type=2)

    params = route.calls.last.request.url.params
    assert params["type"] == "2"
    assert params["Subscription-Key"] == "my-key"


@pytest.mark.asyncio
async def test_invalid_doc_type_rejected_at_call_time() -> None:
    """doc_type must be 1..5 per spec 3-2-1."""
    async with EdinetClient(api_key="my-key") as client:
        with pytest.raises(ValueError, match="doc_type"):
            await client.download_document(doc_id="S1000001", doc_type=99)
