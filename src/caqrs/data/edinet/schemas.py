"""Typed records for EDINET API v2 responses.

Field names follow the official spec (`ESE140206.pdf` section
3-1-2-2). The wire format uses camelCase + ``"0"``/``"1"`` string
flags + JST naive datetimes; the schema converts these to snake_case
+ ``bool`` + Python ``datetime`` at parse time so agents see only the
canonical Python shape.
"""

from __future__ import annotations

from datetime import date as _date
from datetime import datetime
from typing import Annotated, Any, Self

from pydantic import BeforeValidator, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from caqrs.schemas.common import StrictBaseModel


def _str_flag_to_bool(value: Any) -> Any:
    """Wire format encodes booleans as ``"0"``/``"1"`` strings."""
    if isinstance(value, str):
        return value == "1"
    return value


def _empty_string_to_none(value: Any) -> Any:
    """EDINET sometimes serialises absent dates / strings as ``""``."""
    if value == "":
        return None
    return value


_BoolFromStrFlag = Annotated[bool, BeforeValidator(_str_flag_to_bool)]
_OptionalDate = Annotated[_date | None, BeforeValidator(_empty_string_to_none)]
_OptionalStr = Annotated[str | None, BeforeValidator(_empty_string_to_none)]

# Status flag value ranges per spec 3-1-2-2:
# - withdrawalStatus (#32): "0" | "1" (取下書) | "2" (取り下げられた書類)
# - docInfoEditStatus (#33): "0" | "1" (修正情報) | "2" (修正された書類)
# - disclosureStatus (#34): "0" | "1" (不開示開始) | "2" (不開示中) | "3" (解除済)
# - legalStatus (#40): "0" (閲覧期間満了) | "1" (縦覧中) | "2" (延長期間中)
_StatusFlag012 = Annotated[str, Field(pattern=r"^[0-2]$")]
_StatusFlag0123 = Annotated[str, Field(pattern=r"^[0-3]$")]


class EdinetResultset(StrictBaseModel):
    """``metadata.resultset`` block (spec 3-1-2-1 #6-7)."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        # External API boundary — JSON ints arrive as Python ints, so
        # strict here is technically OK, but keep it consistent with
        # the rest of the EDINET schemas.
        strict=False,
        str_strip_whitespace=True,
        populate_by_name=True,
    )

    count: int = Field(ge=0)


class EdinetMetadata(StrictBaseModel):
    """Top-level metadata block (spec 3-1-2-1 #1-10)."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        # External API boundary: JSON has no native datetime/date type,
        # so strict mode would reject ISO strings. The schema validates
        # field shapes / status enum ranges, not Python type identity.
        strict=False,
        str_strip_whitespace=True,
        populate_by_name=True,
        alias_generator=to_camel,
    )

    title: str = Field(min_length=1)
    status: str = Field(pattern=r"^\d{3}$")
    message: str
    resultset: EdinetResultset | None = None
    # Echo of the request parameters — kept for debugging / replay.
    parameter: dict[str, str] | None = None
    # When EDINET processed the request (JST naive); a free-form
    # string because the format varies between gateway versions.
    process_date_time: str | None = None


class EdinetDocument(StrictBaseModel):
    """One document submission record from
    ``GET /documents.json?type=2`` (spec 3-1-2-2).

    Field-by-field correspondence to the upstream camelCase JSON is
    via :class:`pydantic.alias_generators.to_camel`. Boolean flags
    arrive as ``"0"``/``"1"`` strings and are coerced to ``bool``;
    optional dates arrive as ``""`` and are coerced to ``None``.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        # External API boundary: JSON has no native datetime/date type,
        # so strict mode would reject ISO strings. The schema validates
        # field shapes / status enum ranges, not Python type identity.
        strict=False,
        str_strip_whitespace=True,
        populate_by_name=True,
        alias_generator=to_camel,
    )

    seq_number: int = Field(ge=1)
    doc_id: str = Field(min_length=1, max_length=20, alias="docID")
    edinet_code: str = Field(min_length=1, max_length=20)
    sec_code: _OptionalStr = None
    jcn: _OptionalStr = Field(default=None, alias="JCN")
    filer_name: str = Field(min_length=1)
    fund_code: _OptionalStr = None
    ordinance_code: str = Field(min_length=1)
    form_code: str = Field(min_length=1)
    doc_type_code: str = Field(min_length=1)
    period_start: _OptionalDate = None
    period_end: _OptionalDate = None
    submit_date_time: datetime
    doc_description: _OptionalStr = None
    issuer_edinet_code: _OptionalStr = None
    subject_edinet_code: _OptionalStr = None
    # Comma-joined up to 10 EDINET codes (spec #28). Kept as a
    # free-form string; downstream code splits on ',' if needed.
    subsidiary_edinet_code: _OptionalStr = None
    # Comma-joined 提出事由; up to 1000 全半角 chars (spec #29).
    current_report_reason: _OptionalStr = None
    parent_doc_id: _OptionalStr = Field(default=None, alias="parentDocID")
    ope_date_time: datetime | None = None
    withdrawal_status: _StatusFlag012
    doc_info_edit_status: _StatusFlag012
    disclosure_status: _StatusFlag0123
    xbrl_flag: _BoolFromStrFlag
    pdf_flag: _BoolFromStrFlag
    attach_doc_flag: _BoolFromStrFlag
    english_doc_flag: _BoolFromStrFlag
    csv_flag: _BoolFromStrFlag
    legal_status: _StatusFlag012


class EdinetDocumentsList(StrictBaseModel):
    """Top-level response of ``GET /documents.json`` (spec 3-1-2-2)."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        # External API boundary — see EdinetDocument config for
        # rationale. Lets list→tuple coercion work for ``results``.
        strict=False,
        str_strip_whitespace=True,
        populate_by_name=True,
    )

    metadata: EdinetMetadata
    results: tuple[EdinetDocument, ...]

    @model_validator(mode="after")
    def _resultset_count_matches_results(self) -> Self:
        if self.metadata.resultset is None:
            # Error responses from EDINET (spec 3-3 example A) omit
            # the resultset entirely; the client surfaces those as
            # EdinetError and never reaches this validator.
            return self
        if self.metadata.resultset.count != len(self.results):
            raise ValueError(
                f"metadata.resultset.count {self.metadata.resultset.count} "
                f"does not match len(results) {len(self.results)}",
            )
        return self
