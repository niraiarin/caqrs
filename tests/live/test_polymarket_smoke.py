"""Live smoke test for the Polymarket CLOB / Gamma clients.

Gated by ``CAQRS_LIVE=1`` (same convention as the LLM live tests,
declared in ``tests/conftest.py``). No LLM endpoint or API key
required — Polymarket's public read endpoints are unauthenticated.

The test does not assert any content claims; it confirms shape:
the real responses parse into the typed schemas without raising,
which is the early-warning we want for any drift between the
fixture-based unit tests and production responses.

Strategy for picking a market: ``list_markets(active=True, ...)``
and walk down to the first one with at least one non-empty
``clob_token_ids`` entry. Hardcoding a slug or condition id would
break as Polymarket resolves and removes markets over time.
"""

import pytest

from caqrs.data.polymarket import (
    PolymarketClobClient,
    PolymarketGammaClient,
    fetch_polymarket_signal,
)
from caqrs.data.polymarket.clob_client import PriceHistoryInterval, Side
from caqrs.schemas.observer import PolymarketSignal


@pytest.mark.live
@pytest.mark.asyncio
async def test_gamma_list_markets_returns_parseable_results() -> None:
    async with PolymarketGammaClient() as gamma:
        markets = await gamma.list_markets(active=True, closed=False, limit=10)
    assert len(markets) > 0
    # Spot-check the first response parses; every field is optional
    # apart from ``id`` so we only assert structural minimum here.
    first = markets[0]
    assert first.id


@pytest.mark.live
@pytest.mark.asyncio
async def test_clob_endpoints_round_trip_against_real_market() -> None:
    """Pick an active market with populated tokens, query each public
    CLOB endpoint, and confirm the typed schemas parse."""
    async with PolymarketGammaClient() as gamma:
        markets = await gamma.list_markets(active=True, closed=False, limit=20)

    market = next((m for m in markets if m.clob_token_ids), None)
    if market is None:
        pytest.fail(
            "No active Polymarket markets with populated clob_token_ids were "
            "returned — Polymarket likely changed response shape; investigate "
            "before relaxing this guard.",
        )

    token_id = market.clob_token_ids[0]
    async with PolymarketClobClient() as clob:
        midpoint = await clob.get_midpoint(token_id=token_id)
        price = await clob.get_price(token_id=token_id, side=Side.BUY)
        book = await clob.get_orderbook(token_id=token_id)
        history = await clob.get_price_history(
            token_id=token_id,
            interval=PriceHistoryInterval.ONE_DAY,
        )

    # Probability fields are constrained by the schema; if we got
    # here they are within [0, 1]. Just sanity-check the relationships.
    assert 0 <= midpoint <= 1
    assert 0 <= price <= 1
    assert book.asset_id == token_id
    assert history.asset_id == token_id


@pytest.mark.live
@pytest.mark.asyncio
async def test_observer_signal_helper_end_to_end() -> None:
    """fetch_polymarket_signal composes Gamma + CLOB; verify the
    composed result on a real market."""
    async with PolymarketGammaClient() as gamma:
        markets = await gamma.list_markets(active=True, closed=False, limit=20)

    market = next((m for m in markets if m.clob_token_ids and m.slug), None)
    if market is None:
        pytest.fail(
            "No active Polymarket markets with both clob_token_ids and slug "
            "were returned — investigate before relaxing this guard.",
        )

    async with PolymarketGammaClient() as gamma, PolymarketClobClient() as clob:
        # market.slug is non-None per the filter above
        assert market.slug is not None
        signal = await fetch_polymarket_signal(
            gamma_client=gamma,
            clob_client=clob,
            identifier=market.slug,
        )

    assert isinstance(signal, PolymarketSignal)
    assert signal.market_id
    assert len(signal.outcomes) >= 1
    assert signal.fetched_at.tzinfo is not None
