"""Observer-side helpers over :class:`EdinetDbClient`.

EDINET DB serves structured fundamentals (revenue, net income,
EPS / BPS, ROE-like ratios), which are a different shape from the
price-based :class:`AssetSnapshot` produced by J-Quants and
yfinance helpers. This module exposes a parallel
:class:`CompanyFundamentals` snapshot — one record per issuer with
the latest fiscal year + Y/Y deltas — so callers can plug
fundamental signals into the Observer alongside price snapshots.

The helper follows the same "insufficient history → ``None``"
convention as the price helpers: a single available fiscal year
yields ``revenue_growth_yoy = None`` rather than zero, so downstream
agents can distinguish "no data" from "zero growth".
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field

from caqrs.data.edinetdb.client import EdinetDbClient
from caqrs.data.edinetdb.schemas import EdinetDbFinancialPeriod
from caqrs.schemas.common import StrictBaseModel


class CompanyFundamentals(StrictBaseModel):
    """Per-issuer fundamentals snapshot consumed by the Observer.

    All ``latest_*`` fields are ``None`` when the issuer has no
    EDINET DB rows (brand-new listing, unmapped EDINET code, etc.).
    Y/Y growth fields are ``None`` when fewer than two non-null
    fiscal years are available for that metric.

    The full latest-year row is attached as ``latest_period`` so a
    caller that needs cash-flow / employee-count / dividend
    information can read it directly without re-fetching.
    """

    edinet_code: str = Field(min_length=1, max_length=20)
    sec_code: str | None = None

    latest_fiscal_year: int | None = None
    latest_revenue: Decimal | None = None
    latest_net_income: Decimal | None = None
    latest_eps: Decimal | None = None
    latest_bps: Decimal | None = None
    latest_equity_ratio: Decimal | None = None

    revenue_growth_yoy: Decimal | None = None
    net_income_growth_yoy: Decimal | None = None

    latest_period: EdinetDbFinancialPeriod | None = None


async def fetch_edinetdb_company_fundamentals(
    *,
    client: EdinetDbClient,
    edinet_code: str,
) -> CompanyFundamentals:
    """Fetch full fiscal-year history for ``edinet_code`` and reduce
    to a single :class:`CompanyFundamentals` record.

    Walks no extra endpoints beyond
    :meth:`EdinetDbClient.company_financials` — one HTTP call (or
    one cache hit) per call, matching the EDINET DB free-plan
    100-req/day budget.

    Parameters
    ----------
    client:
        An open :class:`EdinetDbClient` (caller owns lifecycle).
    edinet_code:
        EDINET issuer code (``"E02144"`` for Toyota etc.).

    Returns
    -------
    CompanyFundamentals
        With ``latest_*`` populated from the highest ``fiscal_year``
        row, and ``*_growth_yoy`` populated from the
        ``(latest, latest-1)`` pair when both years have non-null,
        non-zero values for that metric.
    """
    history = await client.company_financials(edinet_code=edinet_code)
    if not history:
        return CompanyFundamentals(edinet_code=edinet_code)

    history_sorted = sorted(history, key=lambda r: r.fiscal_year)
    latest = history_sorted[-1]
    prior = history_sorted[-2] if len(history_sorted) >= 2 else None  # noqa: PLR2004

    return CompanyFundamentals(
        edinet_code=edinet_code,
        sec_code=None,  # not on the financials endpoint; caller can join via /companies
        latest_fiscal_year=latest.fiscal_year,
        latest_revenue=latest.revenue,
        latest_net_income=latest.net_income,
        latest_eps=latest.eps,
        latest_bps=latest.bps,
        latest_equity_ratio=latest.equity_ratio_official,
        revenue_growth_yoy=_yoy_growth(
            latest=latest.revenue,
            prior=prior.revenue if prior else None,
        ),
        net_income_growth_yoy=_yoy_growth(
            latest=latest.net_income,
            prior=prior.net_income if prior else None,
        ),
        latest_period=latest,
    )


def _yoy_growth(
    *,
    latest: Decimal | None,
    prior: Decimal | None,
) -> Decimal | None:
    """Return ``(latest - prior) / prior`` when both non-null and
    ``prior`` is non-zero; otherwise ``None``."""
    if latest is None or prior is None or prior == 0:
        return None
    return (latest - prior) / prior
