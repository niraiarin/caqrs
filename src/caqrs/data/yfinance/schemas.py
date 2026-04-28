"""Typed records for Yahoo Finance data.

Field-name drift in upstream yfinance responses ("Net Income" vs
"Net Income Common Stockholders") is collapsed into stable canonical
names at parse time; agents only ever see the canonical names.
"""

from __future__ import annotations

from datetime import date as _date
from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from caqrs.schemas.common import StrictBaseModel


class YFinancePrice(StrictBaseModel):
    """One day's OHLCV bar.

    ``adjusted_close`` is ``None`` when the bar already comes back
    auto-adjusted (i.e. ``auto_adjust=True`` on the upstream call).
    The CAQRS client always passes ``auto_adjust=True`` to avoid the
    split / dividend discontinuities documented in the Zenn pitfalls
    article, so this field is typically ``None`` in practice.
    """

    symbol: str = Field(min_length=1, max_length=20)
    date: _date
    open: Decimal = Field(ge=0)
    high: Decimal = Field(ge=0)
    low: Decimal = Field(ge=0)
    close: Decimal = Field(ge=0)
    adjusted_close: Decimal | None = Field(default=None, ge=0)
    volume: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _high_above_low(self) -> Self:
        if self.high < self.low:
            raise ValueError(
                f"high {self.high} must be >= low {self.low}",
            )
        return self


class YFinanceFinancialPeriod(StrictBaseModel):
    """One reporting period's financial summary.

    Field names are canonical; the client resolves yfinance's
    per-ticker variants ("Net Income" vs "Net Income Common
    Stockholders") before calling the constructor. ``None`` indicates
    the value was missing or NaN in the upstream response.
    """

    symbol: str = Field(min_length=1, max_length=20)
    period_end: _date
    net_income_usd: Decimal | None = None
    total_assets_usd: Decimal | None = None
    total_revenue_usd: Decimal | None = None
