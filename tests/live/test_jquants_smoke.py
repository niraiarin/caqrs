"""Live smoke test for the J-Quants V2 client.

Gated by ``CAQRS_LIVE=1`` plus a non-empty ``JQUANTS_API_KEY`` env
var (the ``jquants_api_key`` fixture in ``tests/live/conftest.py``
deselects when missing). Free-tier registration suffices.

The test exercises the two free-tier endpoints the client wraps and
asserts only that a non-empty typed result comes back; no semantic
assertions on specific tickers / values, so the test stays valid
across publication days and corporate-action revisions.
"""

import pytest

from caqrs.data.jquants import JQuantsClient


@pytest.mark.live
@pytest.mark.asyncio
async def test_list_master_returns_typed_rows(jquants_api_key: str) -> None:
    async with JQuantsClient(api_key=jquants_api_key) as client:
        rows = await client.list_master(code="13010")  # 極洋
    assert len(rows) >= 1
    assert all(r.code.startswith("1301") for r in rows)


@pytest.mark.live
@pytest.mark.asyncio
async def test_daily_bars_returns_typed_rows(jquants_api_key: str) -> None:
    async with JQuantsClient(api_key=jquants_api_key) as client:
        bars = await client.daily_bars(code="13010")
    # Free tier covers a 2-year window with a 12-week delay; for any
    # active ticker we expect at least one bar in the response.
    assert len(bars) >= 1
    sample = bars[0]
    assert sample.code.startswith("1301")
