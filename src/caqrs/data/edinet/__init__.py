"""EDINET data source — JFSA's official corporate-disclosure API.

EDINET (Electronic Disclosure for Investors' NETwork) is the official
JFSA portal for Japan corporate filings: 有価証券報告書,
四半期報告書, 大量保有報告書, 臨時報告書, 公開買付届出書, etc. The
v2 REST API at ``https://api.edinet-fsa.go.jp/api/v2/`` exposes:

- ``GET /documents.json?date=YYYY-MM-DD&type={1|2}`` — documents
  submitted on a given calendar date. ``type=1`` is metadata only;
  ``type=2`` adds per-document status fields.
- ``GET /documents/{docID}?type={1..5}`` — document body. ``type=1``
  returns the XBRL zip; ``type=2`` PDF; ``type=3`` 添付書類 zip;
  ``type=4`` 英文ファイル zip; ``type=5`` CSV.

Authentication: post-2024 EDINET v2 expects a free-tier
``Subscription-Key`` HTTP header registered via the EDINET portal.
The client treats the key as optional so unauthenticated callers still
work (older endpoints / lighter usage).

This subpackage uses only ``httpx`` — no extra dependencies — so it
ships without any optional install gate.
"""

from caqrs.data.edinet.client import EdinetClient, EdinetError
from caqrs.data.edinet.observer_signals import fetch_recent_filings
from caqrs.data.edinet.schemas import (
    EdinetDocument,
    EdinetDocumentsList,
    EdinetMetadata,
    EdinetResultset,
)

__all__ = [
    "EdinetClient",
    "EdinetDocument",
    "EdinetDocumentsList",
    "EdinetError",
    "EdinetMetadata",
    "EdinetResultset",
    "fetch_recent_filings",
]
