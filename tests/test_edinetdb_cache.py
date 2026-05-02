"""TTL SQLite cache for EDINET DB responses.

Required for any production use given the free-plan 100 req/day
quota — the same prefetch + TTL replay pattern that
:mod:`caqrs.data.yfinance.cache` uses for the yfinance scraping
endpoint, applied to EDINET DB's structured-financial endpoints.

Default TTLs reflect upstream change cadence:

- Companies master: 7 days. New listings happen daily but the
  full master is small and stable across consecutive trading days.
- Financials: 30 days. Driven by quarterly filings; mid-quarter
  fiscal-year rows don't change.
- ROE ranking: 7 days. Recomputed at fiscal-year boundaries; the
  list shifts gradually as new annual reports come in.
"""

from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path

import pytest

from caqrs.data.edinetdb.cache import (
    DEFAULT_COMPANIES_TTL_SECONDS,
    DEFAULT_FINANCIALS_TTL_SECONDS,
    DEFAULT_RANKINGS_TTL_SECONDS,
    EdinetDbCache,
)
from caqrs.data.edinetdb.schemas import (
    EdinetDbCompaniesList,
    EdinetDbCompany,
    EdinetDbFinancialPeriod,
    EdinetDbMeta,
    EdinetDbPagination,
    EdinetDbRoeRanking,
)


def _company(*, edinet_code: str = "E03006") -> EdinetDbCompany:
    return EdinetDbCompany(
        edinet_code=edinet_code,
        sec_code="30760",
        name="あいホールディングス株式会社",
        name_en="Ai Holdings Corporation",
        name_ja="あいホールディングス株式会社",
        industry="卸売業",
        accounting_standard="JP",
        credit_rating="S",
        credit_score=93,
    )


def _financial(*, fiscal_year: int = 2024) -> EdinetDbFinancialPeriod:
    return EdinetDbFinancialPeriod(
        accounting_standard="JP",
        fiscal_year=fiscal_year,
        revenue=Decimal("1000"),
        net_income=Decimal("100"),
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
        is_restated_eps=False,
        is_restated_bps=False,
        is_restated_diluted_eps=False,
    )


def _ranking(*, rank: int = 1) -> EdinetDbRoeRanking:
    return EdinetDbRoeRanking(
        edinet_code="E40919",
        sec_code="418A0",
        name="ウリドキ株式会社",
        name_en=None,
        name_ja="ウリドキ株式会社",
        industry="情報・通信業",
        fiscal_year=2025,
        rank=rank,
        value=Decimal("84.39"),
        unit="%",
    )


# === Construction ===


def test_cache_creates_db_lazily(tmp_path: Path) -> None:
    db = tmp_path / "edinetdb-cache.db"
    cache = EdinetDbCache(db_path=db)
    assert not db.exists()
    cache.set_companies(
        page=1,
        per_page=20,
        listing=EdinetDbCompaniesList(
            data=(),
            meta=EdinetDbMeta(pagination=EdinetDbPagination(page=1, per_page=20, total=0)),
        ),
        ttl_seconds=86400,
    )
    assert db.exists()


# === Companies round-trip ===


@pytest.mark.traces("DATA-EDB-A3")
def test_companies_round_trips(tmp_path: Path) -> None:
    cache = EdinetDbCache(db_path=tmp_path / "c.db")
    listing = EdinetDbCompaniesList(
        data=(_company(),),
        meta=EdinetDbMeta(pagination=EdinetDbPagination(page=1, per_page=10, total=1)),
    )
    cache.set_companies(page=1, per_page=10, listing=listing, ttl_seconds=86400)
    fetched = cache.get_companies(page=1, per_page=10)
    assert fetched == listing


def test_companies_miss_returns_none(tmp_path: Path) -> None:
    cache = EdinetDbCache(db_path=tmp_path / "c.db")
    assert cache.get_companies(page=99, per_page=20) is None


def test_companies_pagination_key_distinguishes_pages(tmp_path: Path) -> None:
    """``page`` and ``per_page`` are part of the cache key — paginated
    fetches don't collide."""
    cache = EdinetDbCache(db_path=tmp_path / "c.db")
    listing_p1 = EdinetDbCompaniesList(
        data=(_company(edinet_code="E0001"),),
        meta=EdinetDbMeta(pagination=EdinetDbPagination(page=1, per_page=10, total=2)),
    )
    listing_p2 = EdinetDbCompaniesList(
        data=(_company(edinet_code="E0002"),),
        meta=EdinetDbMeta(pagination=EdinetDbPagination(page=2, per_page=10, total=2)),
    )
    cache.set_companies(page=1, per_page=10, listing=listing_p1, ttl_seconds=86400)
    cache.set_companies(page=2, per_page=10, listing=listing_p2, ttl_seconds=86400)
    assert cache.get_companies(page=1, per_page=10) == listing_p1
    assert cache.get_companies(page=2, per_page=10) == listing_p2
    assert cache.get_companies(page=1, per_page=20) is None  # different per_page → miss


# === Financials round-trip ===


def test_financials_round_trips(tmp_path: Path) -> None:
    cache = EdinetDbCache(db_path=tmp_path / "f.db")
    rows = (_financial(fiscal_year=2023), _financial(fiscal_year=2024))
    cache.set_financials(
        edinet_code="E02144",
        rows=rows,
        ttl_seconds=DEFAULT_FINANCIALS_TTL_SECONDS,
    )
    fetched = cache.get_financials(edinet_code="E02144")
    assert fetched == rows


def test_financials_miss_returns_none(tmp_path: Path) -> None:
    cache = EdinetDbCache(db_path=tmp_path / "f.db")
    assert cache.get_financials(edinet_code="E99999") is None


def test_different_edinet_codes_dont_collide(tmp_path: Path) -> None:
    cache = EdinetDbCache(db_path=tmp_path / "f.db")
    cache.set_financials(
        edinet_code="E02144",
        rows=(_financial(fiscal_year=2024),),
        ttl_seconds=86400,
    )
    cache.set_financials(
        edinet_code="E03006",
        rows=(_financial(fiscal_year=2023),),
        ttl_seconds=86400,
    )
    rows_a = cache.get_financials(edinet_code="E02144")
    rows_b = cache.get_financials(edinet_code="E03006")
    assert rows_a is not None
    assert rows_b is not None
    assert rows_a[0].fiscal_year == 2024
    assert rows_b[0].fiscal_year == 2023


# === Rankings round-trip ===


def test_rankings_round_trips(tmp_path: Path) -> None:
    cache = EdinetDbCache(db_path=tmp_path / "r.db")
    rankings = (_ranking(rank=1), _ranking(rank=2))
    cache.set_rankings(
        endpoint="roe",
        limit=2,
        rows=rankings,
        ttl_seconds=DEFAULT_RANKINGS_TTL_SECONDS,
    )
    fetched = cache.get_rankings(endpoint="roe", limit=2)
    assert fetched == rankings


def test_rankings_endpoint_in_key(tmp_path: Path) -> None:
    """Future endpoints (``per_share_growth`` etc.) reuse the same
    schema; the endpoint string is part of the key so they don't
    collide with /rankings/roe."""
    cache = EdinetDbCache(db_path=tmp_path / "r.db")
    cache.set_rankings(
        endpoint="roe",
        limit=10,
        rows=(_ranking(),),
        ttl_seconds=86400,
    )
    assert cache.get_rankings(endpoint="some_other", limit=10) is None


# === TTL expiry ===


def test_expired_companies_returns_none(tmp_path: Path) -> None:
    cache = EdinetDbCache(db_path=tmp_path / "c.db")
    cache.set_companies(
        page=1,
        per_page=20,
        listing=EdinetDbCompaniesList(
            data=(),
            meta=EdinetDbMeta(pagination=EdinetDbPagination(page=1, per_page=20, total=0)),
        ),
        ttl_seconds=0,
    )
    time.sleep(0.01)
    assert cache.get_companies(page=1, per_page=20) is None


def test_expired_financials_returns_none(tmp_path: Path) -> None:
    cache = EdinetDbCache(db_path=tmp_path / "f.db")
    cache.set_financials(
        edinet_code="E02144",
        rows=(_financial(),),
        ttl_seconds=0,
    )
    time.sleep(0.01)
    assert cache.get_financials(edinet_code="E02144") is None


# === Default TTLs ===


def test_default_ttls_match_documented_cadence() -> None:
    # 7 days for masters / rankings, 30 days for financials.
    assert DEFAULT_COMPANIES_TTL_SECONDS == 7 * 86_400
    assert DEFAULT_FINANCIALS_TTL_SECONDS == 30 * 86_400
    assert DEFAULT_RANKINGS_TTL_SECONDS == 7 * 86_400


# === Persistence across instances ===


def test_cache_persists_across_instances(tmp_path: Path) -> None:
    db = tmp_path / "shared.db"
    rows = (_financial(fiscal_year=2024),)

    writer = EdinetDbCache(db_path=db)
    writer.set_financials(edinet_code="E02144", rows=rows, ttl_seconds=86400)

    reader = EdinetDbCache(db_path=db)
    assert reader.get_financials(edinet_code="E02144") == rows


# === clear() helper ===


def test_clear_drops_every_entry(tmp_path: Path) -> None:
    cache = EdinetDbCache(db_path=tmp_path / "c.db")
    cache.set_companies(
        page=1,
        per_page=20,
        listing=EdinetDbCompaniesList(
            data=(),
            meta=EdinetDbMeta(pagination=EdinetDbPagination(page=1, per_page=20, total=0)),
        ),
        ttl_seconds=86400,
    )
    cache.set_financials(edinet_code="E02144", rows=(_financial(),), ttl_seconds=86400)
    cache.set_rankings(endpoint="roe", limit=10, rows=(_ranking(),), ttl_seconds=86400)
    cache.clear()
    assert cache.get_companies(page=1, per_page=20) is None
    assert cache.get_financials(edinet_code="E02144") is None
    assert cache.get_rankings(endpoint="roe", limit=10) is None
