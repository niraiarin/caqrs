"""Tests for J-Quants typed response schemas."""

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from caqrs.data.jquants import JQuantsDailyBar, JQuantsListedStock

# === JQuantsListedStock ===


def test_listed_stock_parses_documented_column_aliases() -> None:
    raw = {
        "Date": "2025-04-25",
        "Code": "13010",
        "CoName": "極洋",
        "CoNameEn": "KYOKUYO CO.,LTD.",
        "S17": "1",
        "S17Nm": "食品",
        "S33": "0050",
        "S33Nm": "水産・農林業",
        "ScaleCat": "TOPIX Small 1",
        "Mkt": "0111",
        "MktNm": "プライム",
        "Mrgn": "1",
        "MrgnNm": "貸借",
    }
    stock = JQuantsListedStock.model_validate(raw)
    assert stock.date == date(2025, 4, 25)
    assert stock.code == "13010"
    assert stock.company_name == "極洋"
    assert stock.company_name_en == "KYOKUYO CO.,LTD."
    assert stock.sector_17_code == "1"
    assert stock.sector_33_name == "水産・農林業"
    assert stock.market_name == "プライム"
    assert stock.margin_name == "貸借"


def test_listed_stock_tolerates_optional_field_absence() -> None:
    raw = {
        "Date": "2025-04-25",
        "Code": "13010",
        "CoName": "極洋",
    }
    stock = JQuantsListedStock.model_validate(raw)
    assert stock.code == "13010"
    assert stock.company_name_en is None
    assert stock.sector_17_code is None


def test_listed_stock_ignores_unknown_columns() -> None:
    """The API may add fields over time; we don't want forbid behavior here."""
    raw = {
        "Date": "2025-04-25",
        "Code": "13010",
        "CoName": "極洋",
        "FutureNewField": "tolerated",
    }
    stock = JQuantsListedStock.model_validate(raw)
    assert stock.code == "13010"


# === JQuantsDailyBar ===


def test_daily_bar_parses_full_field_set() -> None:
    raw = {
        "Date": "2025-04-25",
        "Code": "13010",
        "O": "100.0",
        "H": "110.0",
        "L": "90.0",
        "C": "105.0",
        "UL": "120.0",
        "LL": "80.0",
        "Vo": "1000",
        "Va": "100000.0",
        "AdjFactor": "1.0",
        "AdjO": "100.0",
        "AdjH": "110.0",
        "AdjL": "90.0",
        "AdjC": "105.0",
        "AdjVo": "1000",
        "MO": None,
        "MH": None,
        "ML": None,
        "MC": None,
        "MUL": None,
        "MLL": None,
        "MVo": None,
        "MVa": None,
    }
    bar = JQuantsDailyBar.model_validate(raw)
    assert bar.date == date(2025, 4, 25)
    assert bar.code == "13010"
    assert bar.open == Decimal("100.0")
    assert bar.high == Decimal("110.0")
    assert bar.low == Decimal("90.0")
    assert bar.close == Decimal("105.0")
    assert bar.upper_limit == Decimal("120.0")
    assert bar.lower_limit == Decimal("80.0")
    assert bar.volume == 1000
    assert bar.trading_value == Decimal("100000.0")
    assert bar.adjusted_close == Decimal("105.0")
    assert bar.morning_close is None


def test_daily_bar_handles_missing_session_data() -> None:
    """Stocks that didn't trade on a date may have null OHLC."""
    raw = {
        "Date": "2025-04-25",
        "Code": "13010",
        "O": None,
        "H": None,
        "L": None,
        "C": None,
        "Vo": 0,
    }
    bar = JQuantsDailyBar.model_validate(raw)
    assert bar.open is None
    assert bar.close is None
    assert bar.volume == 0


def test_daily_bar_rejects_invalid_date() -> None:
    raw = {"Date": "not-a-date", "Code": "13010"}
    with pytest.raises(ValidationError):
        JQuantsDailyBar.model_validate(raw)


def test_daily_bar_is_frozen() -> None:
    raw = {"Date": "2025-04-25", "Code": "13010"}
    bar = JQuantsDailyBar.model_validate(raw)
    with pytest.raises(ValidationError, match="frozen"):
        bar.code = "99999"  # type: ignore[misc]
