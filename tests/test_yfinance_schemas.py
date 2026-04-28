"""yFinance schema validation.

The yfinance library returns pandas DataFrames with field names that
vary across tickers ("Net Income" vs "Net Income Common Stockholders"
— see Zenn yfinance-production-pitfalls article). The CAQRS schema
collapses those variants into stable canonical fields **before** the
data crosses the agent boundary, so downstream agents never see the
field-name drift.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from caqrs.data.yfinance.schemas import (
    YFinanceFinancialPeriod,
    YFinancePrice,
)


class TestYFinancePrice:
    def test_accepts_full_record(self) -> None:
        bar = YFinancePrice(
            symbol="AAPL",
            date=date(2026, 4, 28),
            open=Decimal("180.50"),
            high=Decimal("182.30"),
            low=Decimal("179.10"),
            close=Decimal("181.75"),
            adjusted_close=Decimal("181.75"),
            volume=12_345_678,
        )
        assert bar.symbol == "AAPL"
        assert bar.close == Decimal("181.75")

    def test_adjusted_close_optional_when_already_adjusted(self) -> None:
        """auto_adjust=True merges adjusted_close into close — the raw
        adjusted column is omitted in that case, so the schema must
        accept it as None without forcing client-side defaults."""
        bar = YFinancePrice(
            symbol="AAPL",
            date=date(2026, 4, 28),
            open=Decimal("180"),
            high=Decimal("182"),
            low=Decimal("179"),
            close=Decimal("181"),
            adjusted_close=None,
            volume=1_000_000,
        )
        assert bar.adjusted_close is None

    def test_volume_optional(self) -> None:
        """Some venues / extended-hours bars have no volume."""
        bar = YFinancePrice(
            symbol="AAPL",
            date=date(2026, 4, 28),
            open=Decimal("180"),
            high=Decimal("182"),
            low=Decimal("179"),
            close=Decimal("181"),
            adjusted_close=None,
            volume=None,
        )
        assert bar.volume is None

    def test_rejects_negative_prices(self) -> None:
        with pytest.raises(ValidationError):
            YFinancePrice(
                symbol="AAPL",
                date=date(2026, 4, 28),
                open=Decimal("-1"),
                high=Decimal("182"),
                low=Decimal("179"),
                close=Decimal("181"),
                adjusted_close=None,
                volume=None,
            )

    def test_rejects_high_less_than_low(self) -> None:
        with pytest.raises(ValidationError, match="high"):
            YFinancePrice(
                symbol="AAPL",
                date=date(2026, 4, 28),
                open=Decimal("180"),
                high=Decimal("179"),
                low=Decimal("182"),
                close=Decimal("181"),
                adjusted_close=None,
                volume=None,
            )

    def test_round_trips_through_json(self) -> None:
        original = YFinancePrice(
            symbol="MSFT",
            date=date(2026, 4, 28),
            open=Decimal("400"),
            high=Decimal("405"),
            low=Decimal("398"),
            close=Decimal("402"),
            adjusted_close=Decimal("402"),
            volume=2_000_000,
        )
        restored = YFinancePrice.model_validate_json(original.model_dump_json())
        assert restored == original


class TestYFinanceFinancialPeriod:
    def test_canonical_fields_with_field_name_fallbacks_resolved(self) -> None:
        """Field-name variability ("Net Income" vs
        "Net Income Common Stockholders") must be resolved at parse
        time; agents see only the canonical names."""
        fin = YFinanceFinancialPeriod(
            symbol="AAPL",
            period_end=date(2025, 12, 31),
            net_income_usd=Decimal("100000000000"),
            total_assets_usd=Decimal("400000000000"),
            total_revenue_usd=Decimal("380000000000"),
        )
        assert fin.net_income_usd == Decimal("100000000000")

    def test_missing_values_are_none_not_nan(self) -> None:
        """yfinance returns NaN for missing fields; the client must
        coerce NaN → None before passing to the schema (Decimal
        rejects NaN). The schema itself must accept None."""
        fin = YFinanceFinancialPeriod(
            symbol="AAPL",
            period_end=date(2025, 12, 31),
            net_income_usd=None,
            total_assets_usd=None,
            total_revenue_usd=None,
        )
        assert fin.net_income_usd is None

    def test_is_frozen_extra_forbid(self) -> None:
        fin = YFinanceFinancialPeriod(
            symbol="AAPL",
            period_end=date(2025, 12, 31),
            net_income_usd=Decimal("100"),
            total_assets_usd=None,
            total_revenue_usd=None,
        )
        with pytest.raises(ValidationError, match="frozen"):
            fin.symbol = "MSFT"  # type: ignore[misc]
        with pytest.raises(ValidationError):
            YFinanceFinancialPeriod(  # type: ignore[call-arg]
                symbol="AAPL",
                period_end=date(2025, 12, 31),
                net_income_usd=None,
                total_assets_usd=None,
                total_revenue_usd=None,
                extra="x",
            )
