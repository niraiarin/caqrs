"""EDINET v2 schema validation.

Field names + value semantics follow the official EDINET API v2 spec
(`ESE140206.pdf`, sections 3-1-2-2 / 3-3). Status flags are validated
to their documented value ranges; mismatches surface as
``ValidationError`` rather than silently passing.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from caqrs.data.edinet.schemas import (
    EdinetDocument,
    EdinetDocumentsList,
    EdinetMetadata,
    EdinetResultset,
)


def _full_doc_kwargs() -> dict[str, Any]:
    return {
        "seq_number": 1,
        "doc_id": "S1000001",
        "edinet_code": "E10001",
        "sec_code": "13010",
        "jcn": "6000012010023",
        "filer_name": "極洋株式会社",
        "fund_code": None,
        "ordinance_code": "010",
        "form_code": "030000",
        "doc_type_code": "120",
        "period_start": date(2024, 1, 1),
        "period_end": date(2024, 3, 31),
        "submit_date_time": datetime(2024, 4, 15, 8, 30),
        "doc_description": "第三四半期報告書",
        "issuer_edinet_code": None,
        "subject_edinet_code": None,
        "subsidiary_edinet_code": None,
        "current_report_reason": None,
        "parent_doc_id": None,
        "ope_date_time": None,
        "withdrawal_status": "0",
        "doc_info_edit_status": "0",
        "disclosure_status": "0",
        "xbrl_flag": True,
        "pdf_flag": True,
        "csv_flag": True,
        "attach_doc_flag": False,
        "english_doc_flag": False,
        "legal_status": "1",
    }


# === EdinetDocument ===


class TestEdinetDocument:
    def test_accepts_full_record(self) -> None:
        doc = EdinetDocument(**_full_doc_kwargs())
        assert doc.doc_id == "S1000001"
        assert doc.sec_code == "13010"
        assert doc.xbrl_flag is True
        assert doc.legal_status == "1"

    def test_accepts_optional_fields_as_none(self) -> None:
        kwargs = _full_doc_kwargs()
        kwargs.update(
            sec_code=None,
            jcn=None,
            fund_code="G00001",
            period_start=None,
            period_end=None,
            doc_description=None,
            issuer_edinet_code=None,
            subject_edinet_code=None,
            subsidiary_edinet_code=None,
        )
        doc = EdinetDocument(**kwargs)
        assert doc.sec_code is None
        assert doc.fund_code == "G00001"

    def test_disclosure_status_accepts_3_values(self) -> None:
        """disclosure_status uses 4 values per spec section 3-1-2-2 #34:
        "0" / "1" (不開示開始) / "2" (不開示中) / "3" (解除済)."""
        for value in ("0", "1", "2", "3"):
            kwargs = _full_doc_kwargs()
            kwargs["disclosure_status"] = value
            doc = EdinetDocument(**kwargs)
            assert doc.disclosure_status == value

    def test_withdrawal_status_rejects_3_or_higher(self) -> None:
        """withdrawal_status is 0..2 per spec #32. Any other value is
        upstream protocol drift worth surfacing."""
        kwargs = _full_doc_kwargs()
        kwargs["withdrawal_status"] = "3"
        with pytest.raises(ValidationError):
            EdinetDocument(**kwargs)

    def test_disclosure_status_rejects_4_or_higher(self) -> None:
        kwargs = _full_doc_kwargs()
        kwargs["disclosure_status"] = "4"
        with pytest.raises(ValidationError):
            EdinetDocument(**kwargs)

    def test_legal_status_rejects_3_or_higher(self) -> None:
        """legal_status is 0..2 per spec #40."""
        kwargs = _full_doc_kwargs()
        kwargs["legal_status"] = "5"
        with pytest.raises(ValidationError):
            EdinetDocument(**kwargs)

    def test_subsidiary_edinet_code_accepts_comma_joined(self) -> None:
        """The spec (#28) allows up to 10 EDINET codes joined by ','
        (max 69 chars). The schema keeps it as a free-form string;
        downstream code splits on ',' if needed."""
        kwargs = _full_doc_kwargs()
        kwargs["subsidiary_edinet_code"] = "E10001,E10002,E10003"
        doc = EdinetDocument(**kwargs)
        assert doc.subsidiary_edinet_code == "E10001,E10002,E10003"

    def test_is_frozen_extra_forbid(self) -> None:
        doc = EdinetDocument(**_full_doc_kwargs())
        with pytest.raises(ValidationError, match="frozen"):
            doc.doc_id = "X"  # type: ignore[misc]
        # extra="forbid" rejects unknown fields. Use model_validate so
        # mypy doesn't flag the synthetic kwarg.
        with pytest.raises(ValidationError):
            EdinetDocument.model_validate({**_full_doc_kwargs(), "unknown": "x"})


# === Container ===


class TestEdinetDocumentsList:
    def test_accepts_empty_results(self) -> None:
        listing = EdinetDocumentsList(
            metadata=EdinetMetadata(
                title="EDINET",
                status="200",
                message="OK",
                resultset=EdinetResultset(count=0),
            ),
            results=(),
        )
        assert listing.results == ()
        assert listing.metadata.resultset is not None
        assert listing.metadata.resultset.count == 0

    def test_count_must_match_results_length(self) -> None:
        with pytest.raises(ValidationError, match="count"):
            EdinetDocumentsList(
                metadata=EdinetMetadata(
                    title="EDINET",
                    status="200",
                    message="OK",
                    resultset=EdinetResultset(count=5),
                ),
                results=(),
            )

    def test_round_trip_through_json(self) -> None:
        doc = EdinetDocument(**_full_doc_kwargs())
        listing = EdinetDocumentsList(
            metadata=EdinetMetadata(
                title="EDINET",
                status="200",
                message="OK",
                resultset=EdinetResultset(count=1),
            ),
            results=(doc,),
        )
        restored = EdinetDocumentsList.model_validate_json(listing.model_dump_json())
        assert restored == listing


# === Numeric / Decimal sanity ===


def test_sec_code_5_digits_preserved_as_string() -> None:
    """sec_code is a 4-digit ticker + 1-digit check digit. Keep as
    str so leading zeros aren't dropped (e.g. '13010' = 極洋 stays
    distinguishable from '01301')."""
    doc = EdinetDocument(**_full_doc_kwargs())
    assert isinstance(doc.sec_code, str)
    assert doc.sec_code == "13010"
    assert doc.sec_code != Decimal("13010")
