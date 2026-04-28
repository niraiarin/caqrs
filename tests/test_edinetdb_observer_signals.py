"""EDINET DB observer-signals helper.

The bridge produces a :class:`CompanyFundamentals` snapshot per
issuer: latest fiscal year + Y/Y deltas for revenue, net income,
EPS, and ROE-like metrics. This is the fundamental-analysis
counterpart to the price-based :class:`AssetSnapshot` returned by
J-Quants and yfinance helpers.

EdinetDbFinancialPeriod fields are nullable (older filings often
omit cash-flow and per-share rows); the bridge follows the same
"insufficient history → ``None``" semantics as the price helpers.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
import respx
from pydantic import ValidationError

from caqrs.data.edinetdb import (
    EdinetDbClient,
    EdinetDbFinancialPeriod,
)
from caqrs.data.edinetdb.observer_signals import (
    CompanyFundamentals,
    fetch_edinetdb_company_fundamentals,
)

_BASE = "https://edinetdb.jp/v1"


def _financial_record(
    *,
    fiscal_year: int,
    revenue: float,
    net_income: float,
    eps: float | None = 100.0,
    bps: float | None = 1000.0,
) -> dict[str, object]:
    return {
        "accounting_standard": "JP",
        "fiscal_year": fiscal_year,
        "revenue": revenue,
        "net_income": net_income,
        "ordinary_income": net_income * 1.1,
        "comprehensive_income": net_income,
        "total_assets": revenue * 2,
        "total_liabilities": revenue * 0.5,
        "net_assets": revenue * 1.5,
        "cash": revenue * 0.3,
        "cf_operating": net_income * 1.2,
        "cf_investing": -net_income * 0.5,
        "cf_financing": -net_income * 0.3,
        "eps": eps,
        "bps": bps,
        "adjusted_eps": eps,
        "adjusted_bps": bps,
        "adjusted_dividend_per_share": 10.0,
        "dividend_per_share": 10.0,
        "equity_ratio_official": 0.75,
        "shares_issued": 1_000_000.0,
        "split_adjustment_factor": 1.0,
        "num_employees": 100,
        "temp_employees": 10,
        "is_restated_eps": False,
        "is_restated_bps": False,
        "is_restated_diluted_eps": False,
    }


def _financials_payload(rows: list[dict[str, object]]) -> dict[str, object]:
    return {"data": rows}


# === Construction & schema ===


def test_company_fundamentals_is_frozen() -> None:
    snap = CompanyFundamentals(
        edinet_code="E02144",
        sec_code=None,
        latest_fiscal_year=2024,
        latest_revenue=Decimal("1000"),
        latest_net_income=Decimal("100"),
        latest_eps=Decimal("100.0"),
        latest_bps=Decimal("1000.0"),
        latest_equity_ratio=Decimal("0.75"),
        revenue_growth_yoy=Decimal("0.10"),
        net_income_growth_yoy=Decimal("0.20"),
        latest_period=None,
    )
    with pytest.raises(ValidationError, match="frozen"):
        snap.edinet_code = "X"  # type: ignore[misc]


# === Single-period: no Y/Y deltas ===


@pytest.mark.asyncio
@respx.mock
async def test_single_year_returns_no_growth_metrics() -> None:
    """Less than two fiscal years → revenue_growth_yoy /
    net_income_growth_yoy degrade to None rather than zero. Mirrors
    the J-Quants helper's "insufficient history" convention."""
    payload = _financials_payload(
        [_financial_record(fiscal_year=2024, revenue=1000, net_income=100)],
    )
    respx.get(f"{_BASE}/companies/E02144/financials").mock(
        return_value=httpx.Response(200, json=payload),
    )

    async with EdinetDbClient(api_key="k", throttle_seconds=0) as client:
        snap = await fetch_edinetdb_company_fundamentals(
            client=client,
            edinet_code="E02144",
        )

    assert snap.latest_fiscal_year == 2024
    assert snap.latest_revenue == Decimal("1000")
    assert snap.latest_net_income == Decimal("100")
    assert snap.revenue_growth_yoy is None
    assert snap.net_income_growth_yoy is None


# === Two-year history: Y/Y delta computed ===


@pytest.mark.asyncio
@respx.mock
async def test_two_year_history_computes_yoy_growth() -> None:
    payload = _financials_payload(
        [
            _financial_record(fiscal_year=2023, revenue=1000, net_income=100),
            _financial_record(fiscal_year=2024, revenue=1100, net_income=130),
        ],
    )
    respx.get(f"{_BASE}/companies/E02144/financials").mock(
        return_value=httpx.Response(200, json=payload),
    )

    async with EdinetDbClient(api_key="k", throttle_seconds=0) as client:
        snap = await fetch_edinetdb_company_fundamentals(
            client=client,
            edinet_code="E02144",
        )

    # Picks the LATEST year regardless of input order.
    assert snap.latest_fiscal_year == 2024
    # Revenue YoY: (1100 - 1000) / 1000.
    assert snap.revenue_growth_yoy == Decimal("0.1")
    # Net income YoY: (130 - 100) / 100.
    assert snap.net_income_growth_yoy == Decimal("0.3")


@pytest.mark.asyncio
@respx.mock
async def test_picks_latest_year_when_input_is_unsorted() -> None:
    """Order independence — defends against EDINET DB returning
    rows in arbitrary order."""
    payload = _financials_payload(
        [
            _financial_record(fiscal_year=2024, revenue=1100, net_income=130),
            _financial_record(fiscal_year=2022, revenue=900, net_income=90),
            _financial_record(fiscal_year=2023, revenue=1000, net_income=100),
        ],
    )
    respx.get(f"{_BASE}/companies/E02144/financials").mock(
        return_value=httpx.Response(200, json=payload),
    )

    async with EdinetDbClient(api_key="k", throttle_seconds=0) as client:
        snap = await fetch_edinetdb_company_fundamentals(
            client=client,
            edinet_code="E02144",
        )

    assert snap.latest_fiscal_year == 2024
    # Y/Y still uses 2023 -> 2024 (the immediately-prior year).
    assert snap.revenue_growth_yoy == Decimal("0.1")


# === Empty / missing data ===


@pytest.mark.asyncio
@respx.mock
async def test_no_financials_returns_empty_snapshot() -> None:
    """Brand-new listings sometimes return an empty data array.
    The helper returns a snapshot with all latest_* fields None
    rather than raising, so a screening loop can keep going."""
    payload: dict[str, object] = {"data": []}
    respx.get(f"{_BASE}/companies/E99999/financials").mock(
        return_value=httpx.Response(200, json=payload),
    )

    async with EdinetDbClient(api_key="k", throttle_seconds=0) as client:
        snap = await fetch_edinetdb_company_fundamentals(
            client=client,
            edinet_code="E99999",
        )

    assert snap.edinet_code == "E99999"
    assert snap.latest_fiscal_year is None
    assert snap.latest_revenue is None
    assert snap.latest_net_income is None
    assert snap.revenue_growth_yoy is None


# === Null pivot values do not break Y/Y ===


@pytest.mark.asyncio
@respx.mock
async def test_yoy_is_none_when_prior_year_has_null_revenue() -> None:
    """Older fiscal years sometimes report null revenue. Y/Y growth
    needs a non-null, non-zero denominator; otherwise the helper
    degrades that single metric to None."""
    null_revenue_2023: dict[str, object] = {
        "accounting_standard": "JP",
        "fiscal_year": 2023,
        "revenue": None,
        "net_income": 100,
        "ordinary_income": None,
        "comprehensive_income": None,
        "total_assets": None,
        "total_liabilities": None,
        "net_assets": None,
        "cash": None,
        "cf_operating": None,
        "cf_investing": None,
        "cf_financing": None,
        "eps": None,
        "bps": None,
        "adjusted_eps": None,
        "adjusted_bps": None,
        "adjusted_dividend_per_share": None,
        "dividend_per_share": None,
        "equity_ratio_official": None,
        "shares_issued": None,
        "split_adjustment_factor": None,
        "num_employees": None,
        "temp_employees": None,
        "is_restated_eps": False,
        "is_restated_bps": False,
        "is_restated_diluted_eps": False,
    }
    payload = _financials_payload(
        [
            _financial_record(fiscal_year=2024, revenue=1100, net_income=130),
            null_revenue_2023,
        ],
    )
    respx.get(f"{_BASE}/companies/E02144/financials").mock(
        return_value=httpx.Response(200, json=payload),
    )

    async with EdinetDbClient(api_key="k", throttle_seconds=0) as client:
        snap = await fetch_edinetdb_company_fundamentals(
            client=client,
            edinet_code="E02144",
        )

    # Revenue Y/Y null because 2023 revenue was null.
    assert snap.revenue_growth_yoy is None
    # Net income Y/Y still computable (both years had a non-null value).
    assert snap.net_income_growth_yoy == Decimal("0.3")


# === Latest period round-trip ===


@pytest.mark.asyncio
@respx.mock
async def test_latest_period_attached_for_caller_inspection() -> None:
    """Callers that want fields beyond the headline metrics (CF,
    employee count, etc.) can read the full latest-year row from
    snap.latest_period."""
    payload = _financials_payload(
        [_financial_record(fiscal_year=2024, revenue=1000, net_income=100)],
    )
    respx.get(f"{_BASE}/companies/E02144/financials").mock(
        return_value=httpx.Response(200, json=payload),
    )

    async with EdinetDbClient(api_key="k", throttle_seconds=0) as client:
        snap = await fetch_edinetdb_company_fundamentals(
            client=client,
            edinet_code="E02144",
        )

    assert isinstance(snap.latest_period, EdinetDbFinancialPeriod)
    assert snap.latest_period.cf_operating is not None
    assert snap.latest_period.num_employees == 100
