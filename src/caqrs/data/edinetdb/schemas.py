"""Typed records for EDINET DB (edinetdb.jp) v1 responses.

External-API boundary — schemas use ``strict=False`` so the live JSON
payloads (numerics returned as floats / Decimal-string mix) coerce
into ``Decimal`` cleanly. The CAQRS-internal cycle pipeline still uses
strict Pydantic via :class:`StrictBaseModel` for cycle artifacts; only
this external-data boundary opts out.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import ConfigDict, Field

from caqrs.schemas.common import StrictBaseModel


class EdinetDbCompany(StrictBaseModel):
    """One row from ``GET /v1/companies``.

    ``credit_rating`` is a single-letter grade (S / A / B / C / D in
    observed live data); ``credit_score`` is 0-100. ``sec_code`` is
    ``None`` for unlisted entities.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="ignore",
        strict=False,
        str_strip_whitespace=True,
        populate_by_name=True,
    )

    edinet_code: str = Field(min_length=1, max_length=20)
    sec_code: str | None = None
    # JCN (Japan Corporate Number, 13-digit) is optional on the upstream
    # response. Phase E3 reconcile_from_edinetdb_companies populates an
    # `(IdentifierKind.JCN, ...)` identifier when the field is present;
    # absence is the common case for funds / SPCs (see ENT-RECON-T29).
    jcn: str | None = None
    name: str
    name_en: str | None = None
    name_ja: str
    industry: str
    accounting_standard: str
    credit_rating: str | None = Field(default=None, pattern=r"^[A-Z]$")
    credit_score: int | None = Field(default=None, ge=0, le=100)


class EdinetDbFinancialPeriod(StrictBaseModel):
    """One fiscal-year row from
    ``GET /v1/companies/{edinet_code}/financials``.

    Older fiscal years often have ``None`` for many fields (pre-CF-
    disclosure mandates, pre-IFRS adoption, etc.). The schema is
    permissive about absence. ``equity_ratio_official`` is a fraction
    (0-1), not a percent.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="ignore",
        strict=False,
        str_strip_whitespace=True,
        populate_by_name=True,
    )

    accounting_standard: str
    fiscal_year: int = Field(ge=1990, le=2100)

    # P&L
    revenue: Decimal | None = None
    ordinary_income: Decimal | None = None
    net_income: Decimal | None = None
    comprehensive_income: Decimal | None = None

    # Balance sheet
    total_assets: Decimal | None = None
    total_liabilities: Decimal | None = None
    net_assets: Decimal | None = None
    cash: Decimal | None = None

    # Cash flow
    cf_operating: Decimal | None = None
    cf_investing: Decimal | None = None
    cf_financing: Decimal | None = None

    # Per-share
    eps: Decimal | None = None
    bps: Decimal | None = None
    adjusted_eps: Decimal | None = None
    adjusted_bps: Decimal | None = None
    dividend_per_share: Decimal | None = None
    adjusted_dividend_per_share: Decimal | None = None

    # Other ratios / counts
    equity_ratio_official: Decimal | None = None
    shares_issued: Decimal | None = None
    split_adjustment_factor: Decimal | None = None
    num_employees: int | None = None
    temp_employees: int | None = None

    # Restatement flags
    is_restated_eps: bool = False
    is_restated_bps: bool = False
    is_restated_diluted_eps: bool = False


class EdinetDbRoeRanking(StrictBaseModel):
    """One row from ``GET /v1/rankings/roe``.

    The same row shape is reused for other ``/rankings/*`` endpoints;
    ``unit`` distinguishes percent from other scales.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="ignore",
        strict=False,
        str_strip_whitespace=True,
        populate_by_name=True,
    )

    edinet_code: str
    sec_code: str | None = None
    name: str
    name_en: str | None = None
    name_ja: str
    industry: str
    fiscal_year: int = Field(ge=1990, le=2100)
    rank: int = Field(ge=1)
    value: Decimal
    unit: str


class EdinetDbPagination(StrictBaseModel):
    """``meta.pagination`` block returned by paginated endpoints."""

    model_config = ConfigDict(
        frozen=True,
        extra="ignore",
        strict=False,
        str_strip_whitespace=True,
        populate_by_name=True,
    )

    page: int = Field(ge=1)
    per_page: int = Field(ge=1)
    total: int | None = Field(default=None, ge=0)


class EdinetDbMeta(StrictBaseModel):
    """``meta`` block returned by paginated endpoints — wraps
    :class:`EdinetDbPagination` plus any future top-level metadata."""

    model_config = ConfigDict(
        frozen=True,
        extra="ignore",
        strict=False,
        str_strip_whitespace=True,
        populate_by_name=True,
    )

    pagination: EdinetDbPagination


class EdinetDbCompaniesList(StrictBaseModel):
    """Top-level response of ``GET /v1/companies``."""

    model_config = ConfigDict(
        frozen=True,
        extra="ignore",
        strict=False,
        str_strip_whitespace=True,
        populate_by_name=True,
    )

    data: tuple[EdinetDbCompany, ...]
    meta: EdinetDbMeta
