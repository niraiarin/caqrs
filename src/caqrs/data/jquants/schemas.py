"""Typed J-Quants response artifacts.

J-Quants returns short-form column names (``Date``, ``Code``, ``O``,
``H``, ``L``, ``C``, ``Vo``, ``Va``, ``AdjFactor``, etc.). We expose
pythonic snake_case attribute names with field aliases so models
parse the raw upstream JSON without a separate translation step,
while downstream code stays readable.

Frozen + ``populate_by_name=True`` so callers may construct rows
with either alias or python name (handy for tests). ``extra="ignore"``
because J-Quants may add columns over time and we don't want a
schema bump every release; if a new column matters we'll alias it
explicitly.
"""

from datetime import date as date_
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class JQuantsBaseModel(BaseModel):
    """Base for J-Quants typed rows.

    Differs from :class:`caqrs.schemas.common.StrictBaseModel` in two
    ways: (1) ``extra="ignore"`` instead of ``"forbid"`` so an
    upstream-side column addition does not break parsing, and
    (2) non-strict mode so ISO-date strings and decimal-string fields
    coerce automatically. The wire format is well-defined and we are
    not validating user input."""

    model_config = ConfigDict(
        frozen=True,
        extra="ignore",
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class JQuantsListedStock(JQuantsBaseModel):
    """One row from ``GET /v2/equities/master`` — listed-stock metadata."""

    date: date_ = Field(alias="Date")
    code: str = Field(alias="Code", min_length=1)
    company_name: str = Field(alias="CoName")
    company_name_en: str | None = Field(default=None, alias="CoNameEn")
    sector_17_code: str | None = Field(default=None, alias="S17")
    sector_17_name: str | None = Field(default=None, alias="S17Nm")
    sector_33_code: str | None = Field(default=None, alias="S33")
    sector_33_name: str | None = Field(default=None, alias="S33Nm")
    scale_category: str | None = Field(default=None, alias="ScaleCat")
    market_code: str | None = Field(default=None, alias="Mkt")
    market_name: str | None = Field(default=None, alias="MktNm")
    margin_code: str | None = Field(default=None, alias="Mrgn")
    margin_name: str | None = Field(default=None, alias="MrgnNm")


class JQuantsDailyBar(JQuantsBaseModel):
    """One row from ``GET /v2/equities/bars/daily`` — daily OHLCV.

    Adjusted variants apply per-symbol corporate-action factors;
    morning-session columns are populated only on partial-trading
    days (e.g. half-day sessions). All numeric fields are nullable
    because suspended / untraded stocks may have no quote on a
    given date.
    """

    date: date_ = Field(alias="Date")
    code: str = Field(alias="Code", min_length=1)

    open: Decimal | None = Field(default=None, alias="O")
    high: Decimal | None = Field(default=None, alias="H")
    low: Decimal | None = Field(default=None, alias="L")
    close: Decimal | None = Field(default=None, alias="C")
    upper_limit: Decimal | None = Field(default=None, alias="UL")
    lower_limit: Decimal | None = Field(default=None, alias="LL")
    volume: int | None = Field(default=None, alias="Vo")
    trading_value: Decimal | None = Field(default=None, alias="Va")

    adjustment_factor: Decimal | None = Field(default=None, alias="AdjFactor")
    adjusted_open: Decimal | None = Field(default=None, alias="AdjO")
    adjusted_high: Decimal | None = Field(default=None, alias="AdjH")
    adjusted_low: Decimal | None = Field(default=None, alias="AdjL")
    adjusted_close: Decimal | None = Field(default=None, alias="AdjC")
    adjusted_volume: int | None = Field(default=None, alias="AdjVo")

    morning_open: Decimal | None = Field(default=None, alias="MO")
    morning_high: Decimal | None = Field(default=None, alias="MH")
    morning_low: Decimal | None = Field(default=None, alias="ML")
    morning_close: Decimal | None = Field(default=None, alias="MC")
    morning_upper_limit: Decimal | None = Field(default=None, alias="MUL")
    morning_lower_limit: Decimal | None = Field(default=None, alias="MLL")
    morning_volume: int | None = Field(default=None, alias="MVo")
    morning_trading_value: Decimal | None = Field(default=None, alias="MVa")
