"""EDINET DB (edinetdb.jp) schema validation.

Field names + types follow the live API responses. Schemas are
``StrictBaseModel`` subclasses but use ``strict=False`` because the
JSON boundary needs string→Decimal coercion (the API returns numeric
fields as floats / strings depending on the endpoint).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from caqrs.data.edinetdb.schemas import (
    EdinetDbCompany,
    EdinetDbFinancialPeriod,
    EdinetDbRoeRanking,
)

# === EdinetDbCompany ===


class TestEdinetDbCompany:
    def test_accepts_full_record(self) -> None:
        company = EdinetDbCompany(
            edinet_code="E03006",
            sec_code="30760",
            name="あいホールディングス株式会社",
            name_en="Ai Holdings Corporation",
            name_ja="あいホールディングス株式会社",
            industry="卸売業",
            accounting_standard="JP",
            credit_rating="S",
            credit_score=93,
        )
        assert company.edinet_code == "E03006"
        assert company.credit_score == 93

    def test_optional_fields_can_be_none(self) -> None:
        """sec_code is None for unlisted entities; credit_rating /
        credit_score appear only on full company records."""
        company = EdinetDbCompany(
            edinet_code="E03006",
            sec_code=None,
            name="X",
            name_en=None,
            name_ja="X",
            industry="その他",
            accounting_standard="JP",
            credit_rating=None,
            credit_score=None,
        )
        assert company.sec_code is None
        assert company.credit_score is None

    def test_credit_rating_pattern(self) -> None:
        """Credit rating is a single-letter grade A-S in the live data."""
        for grade in ("S", "A", "B", "C", "D"):
            company = EdinetDbCompany(
                edinet_code="E03006",
                sec_code=None,
                name="X",
                name_en=None,
                name_ja="X",
                industry="その他",
                accounting_standard="JP",
                credit_rating=grade,
                credit_score=50,
            )
            assert company.credit_rating == grade

    def test_credit_score_range(self) -> None:
        with pytest.raises(ValidationError):
            EdinetDbCompany(
                edinet_code="E03006",
                sec_code=None,
                name="X",
                name_en=None,
                name_ja="X",
                industry="その他",
                accounting_standard="JP",
                credit_rating=None,
                credit_score=-1,
            )
        with pytest.raises(ValidationError):
            EdinetDbCompany(
                edinet_code="E03006",
                sec_code=None,
                name="X",
                name_en=None,
                name_ja="X",
                industry="その他",
                accounting_standard="JP",
                credit_rating=None,
                credit_score=101,
            )


# === EdinetDbFinancialPeriod ===


class TestEdinetDbFinancialPeriod:
    def test_accepts_full_record(self) -> None:
        fin = EdinetDbFinancialPeriod(
            accounting_standard="JP",
            fiscal_year=2012,
            revenue=Decimal("647652000000"),
            net_income=Decimal("-43204000000"),
            ordinary_income=Decimal("-60863000000"),
            comprehensive_income=Decimal("-51045000000"),
            total_assets=Decimal("1368401000000"),
            total_liabilities=Decimal("177376000000"),
            net_assets=Decimal("1191025000000"),
            cash=Decimal("407186000000"),
            cf_operating=Decimal("-94955000000"),
            cf_investing=Decimal("-164392000000"),
            cf_financing=Decimal("-39823000000"),
            eps=Decimal("-337.86"),
            bps=Decimal("9313.15"),
            adjusted_eps=Decimal("-30.496438882183114"),
            adjusted_bps=Decimal("931.3149999999998"),
            adjusted_dividend_per_share=Decimal("9.999999999999998"),
            dividend_per_share=Decimal("100.0"),
            equity_ratio_official=Decimal("0.8703"),
            shares_issued=Decimal("141669000"),
            split_adjustment_factor=Decimal("10.0"),
            num_employees=4928,
            temp_employees=197,
            is_restated_bps=False,
            is_restated_diluted_eps=False,
            is_restated_eps=False,
        )
        assert fin.fiscal_year == 2012
        assert fin.equity_ratio_official == Decimal("0.8703")

    def test_missing_optional_numeric_fields_become_none(self) -> None:
        """Some fields may be unreported for older fiscal years
        (e.g. cf_* before cash-flow disclosure was mandatory)."""
        fin = EdinetDbFinancialPeriod(
            accounting_standard="JP",
            fiscal_year=2005,
            revenue=Decimal("1000"),
            net_income=None,
            ordinary_income=None,
            comprehensive_income=None,
            total_assets=None,
            total_liabilities=None,
            net_assets=None,
            cash=None,
            cf_operating=None,
            cf_investing=None,
            cf_financing=None,
            eps=None,
            bps=None,
            adjusted_eps=None,
            adjusted_bps=None,
            adjusted_dividend_per_share=None,
            dividend_per_share=None,
            equity_ratio_official=None,
            shares_issued=None,
            split_adjustment_factor=None,
            num_employees=None,
            temp_employees=None,
            is_restated_bps=False,
            is_restated_diluted_eps=False,
            is_restated_eps=False,
        )
        assert fin.cf_operating is None

    def test_fiscal_year_range(self) -> None:
        """EDINET DB reaches back to ~2000 and forward to current.
        Out-of-range values likely indicate caller bugs."""
        with pytest.raises(ValidationError):
            _financial_with(fiscal_year=1900)
        with pytest.raises(ValidationError):
            _financial_with(fiscal_year=3000)


def _financial_with(*, fiscal_year: int) -> EdinetDbFinancialPeriod:
    return EdinetDbFinancialPeriod(
        accounting_standard="JP",
        fiscal_year=fiscal_year,
        revenue=Decimal("0"),
        net_income=None,
        ordinary_income=None,
        comprehensive_income=None,
        total_assets=None,
        total_liabilities=None,
        net_assets=None,
        cash=None,
        cf_operating=None,
        cf_investing=None,
        cf_financing=None,
        eps=None,
        bps=None,
        adjusted_eps=None,
        adjusted_bps=None,
        adjusted_dividend_per_share=None,
        dividend_per_share=None,
        equity_ratio_official=None,
        shares_issued=None,
        split_adjustment_factor=None,
        num_employees=None,
        temp_employees=None,
        is_restated_bps=False,
        is_restated_diluted_eps=False,
        is_restated_eps=False,
    )


# === EdinetDbRoeRanking ===


class TestEdinetDbRoeRanking:
    def test_accepts_full_record(self) -> None:
        rank = EdinetDbRoeRanking(
            edinet_code="E40919",
            sec_code="418A0",
            name="ウリドキ株式会社",
            name_en=None,
            name_ja="ウリドキ株式会社",
            industry="情報・通信業",
            fiscal_year=2025,
            rank=1,
            value=Decimal("84.3889"),
            unit="%",
        )
        assert rank.rank == 1
        assert rank.value == Decimal("84.3889")

    def test_rank_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            EdinetDbRoeRanking(
                edinet_code="E1",
                sec_code=None,
                name="X",
                name_en=None,
                name_ja="X",
                industry="X",
                fiscal_year=2025,
                rank=0,
                value=Decimal("1"),
                unit="%",
            )
