"""Tests for the Polymarket → Observer integration.

Covers (1) the PolymarketSignal / PolymarketOutcome schemas,
(2) the fetch_polymarket_signal helper that composes Gamma + CLOB,
and (3) ObserverAgent.build_user_message including the formatted
block when signals are present.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx
from pydantic import ValidationError

from caqrs.agents.observer import ObserverAgent
from caqrs.data.polymarket import (
    PolymarketClobClient,
    PolymarketGammaClient,
    fetch_polymarket_signal,
    fetch_polymarket_signals,
)
from caqrs.schemas.observer import (
    DataDimension,
    ObserverInput,
    PolymarketOutcome,
    PolymarketSignal,
)

_GAMMA = "https://gamma-api.polymarket.com"
_CLOB = "https://clob.polymarket.com"


# === Fixtures ===


def _market_payload(
    *,
    market_id: str = "12345",
    slug: str = "fed-cuts-2026",
    question: str = "Will the Fed cut in 2026?",
    outcomes: str = '["Yes", "No"]',
    clob_token_ids: str = '["100", "200"]',
    outcome_prices: str = '["0.62", "0.38"]',
) -> dict[str, object]:
    return {
        "id": market_id,
        "slug": slug,
        "question": question,
        "outcomes": outcomes,
        "clobTokenIds": clob_token_ids,
        "outcomePrices": outcome_prices,
        "endDate": "2026-12-31T23:59:59Z",
        "active": True,
        "closed": False,
    }


def _book_payload(*, token_id: str, midpoint: str, last_trade: str) -> dict[str, object]:
    """Realistic /book response shape — bids/asks bracket the midpoint."""
    mid = Decimal(midpoint)
    bid = mid - Decimal("0.01")
    ask = mid + Decimal("0.01")
    return {
        "market": "0xmarket",
        "asset_id": token_id,
        "timestamp": "1735689600",
        "bids": [{"price": str(bid), "size": "100"}],
        "asks": [{"price": str(ask), "size": "100"}],
        "min_order_size": "1",
        "tick_size": "0.01",
        "neg_risk": False,
        "last_trade_price": last_trade,
    }


# === Schema validation ===


def test_polymarket_outcome_rejects_probability_above_one() -> None:
    with pytest.raises(ValidationError):
        PolymarketOutcome(
            label="Yes",
            token_id="100",
            midpoint=Decimal("1.5"),
        )


def test_polymarket_signal_requires_tz_aware_fetched_at() -> None:
    with pytest.raises(ValidationError):
        PolymarketSignal(
            market_id="12345",
            slug="x",
            question="?",
            end_date=None,
            is_binary=True,
            outcomes=(
                PolymarketOutcome(label="Yes", token_id="100"),
                PolymarketOutcome(label="No", token_id="200"),
            ),
            fetched_at=datetime(2026, 1, 1),  # naive
        )


def test_polymarket_signal_binary_must_have_two_outcomes() -> None:
    with pytest.raises(ValidationError, match="is_binary"):
        PolymarketSignal(
            market_id="12345",
            slug=None,
            question=None,
            end_date=None,
            is_binary=True,
            outcomes=(
                PolymarketOutcome(label="A", token_id="1"),
                PolymarketOutcome(label="B", token_id="2"),
                PolymarketOutcome(label="C", token_id="3"),
            ),
            fetched_at=datetime.now(UTC),
        )


# === fetch_polymarket_signal ===


@pytest.mark.asyncio
@respx.mock
async def test_fetch_signal_combines_metadata_and_pricing() -> None:
    respx.get(f"{_GAMMA}/markets", params={"slug": "fed-cuts-2026"}).mock(
        return_value=httpx.Response(200, json=[_market_payload()]),
    )
    respx.get(f"{_CLOB}/midpoint", params={"token_id": "100"}).mock(
        return_value=httpx.Response(200, json={"mid_price": "0.62"}),
    )
    respx.get(f"{_CLOB}/midpoint", params={"token_id": "200"}).mock(
        return_value=httpx.Response(200, json={"mid_price": "0.38"}),
    )
    respx.get(f"{_CLOB}/book", params={"token_id": "100"}).mock(
        return_value=httpx.Response(
            200,
            json=_book_payload(token_id="100", midpoint="0.62", last_trade="0.62"),
        ),
    )
    respx.get(f"{_CLOB}/book", params={"token_id": "200"}).mock(
        return_value=httpx.Response(
            200,
            json=_book_payload(token_id="200", midpoint="0.38", last_trade="0.38"),
        ),
    )

    async with PolymarketGammaClient() as gamma, PolymarketClobClient() as clob:
        signal = await fetch_polymarket_signal(
            gamma_client=gamma,
            clob_client=clob,
            identifier="fed-cuts-2026",
        )

    assert signal.market_id == "12345"
    assert signal.slug == "fed-cuts-2026"
    assert signal.is_binary is True
    assert len(signal.outcomes) == 2
    yes = signal.outcomes[0]
    assert yes.label == "Yes"
    assert yes.token_id == "100"
    assert yes.midpoint == Decimal("0.62")
    assert yes.last_trade_price == Decimal("0.62")
    assert yes.spread == Decimal("0.02")
    assert signal.fetched_at.tzinfo is not None


@pytest.mark.asyncio
@respx.mock
async def test_fetch_signal_degrades_gracefully_on_clob_failure() -> None:
    """Missing midpoint / book on one outcome must not abort the whole signal."""
    respx.get(f"{_GAMMA}/markets", params={"slug": "fed-cuts-2026"}).mock(
        return_value=httpx.Response(200, json=[_market_payload()]),
    )
    # Token 100 succeeds
    respx.get(f"{_CLOB}/midpoint", params={"token_id": "100"}).mock(
        return_value=httpx.Response(200, json={"mid_price": "0.62"}),
    )
    respx.get(f"{_CLOB}/book", params={"token_id": "100"}).mock(
        return_value=httpx.Response(
            200,
            json=_book_payload(token_id="100", midpoint="0.62", last_trade="0.62"),
        ),
    )
    # Token 200 returns 404 on both
    respx.get(f"{_CLOB}/midpoint", params={"token_id": "200"}).mock(
        return_value=httpx.Response(404, text="not found"),
    )
    respx.get(f"{_CLOB}/book", params={"token_id": "200"}).mock(
        return_value=httpx.Response(404, text="not found"),
    )

    async with PolymarketGammaClient() as gamma, PolymarketClobClient() as clob:
        signal = await fetch_polymarket_signal(
            gamma_client=gamma,
            clob_client=clob,
            identifier="fed-cuts-2026",
        )

    assert signal.outcomes[0].midpoint == Decimal("0.62")
    assert signal.outcomes[1].midpoint is None
    assert signal.outcomes[1].spread is None
    assert signal.outcomes[1].last_trade_price is None


@pytest.mark.asyncio
@respx.mock
async def test_fetch_signal_market_without_tokens_yields_placeholder() -> None:
    """A market that exists in Gamma but has no CLOB tokens yet."""
    payload = _market_payload(
        outcomes="[]",
        clob_token_ids="[]",
        outcome_prices="[]",
    )
    respx.get(f"{_GAMMA}/markets", params={"slug": "empty-market"}).mock(
        return_value=httpx.Response(200, json=[payload]),
    )
    async with PolymarketGammaClient() as gamma, PolymarketClobClient() as clob:
        signal = await fetch_polymarket_signal(
            gamma_client=gamma,
            clob_client=clob,
            identifier="empty-market",
        )
    assert signal.is_binary is False
    assert len(signal.outcomes) == 1
    assert signal.outcomes[0].midpoint is None


@pytest.mark.asyncio
@respx.mock
async def test_fetch_polymarket_signals_returns_tuple_in_input_order() -> None:
    for slug, mid_yes in [("a", "0.20"), ("b", "0.80")]:
        respx.get(f"{_GAMMA}/markets", params={"slug": slug}).mock(
            return_value=httpx.Response(
                200,
                json=[
                    _market_payload(
                        market_id=f"id-{slug}",
                        slug=slug,
                        outcome_prices=f'["{mid_yes}", "0.5"]',
                    ),
                ],
            ),
        )
        respx.get(f"{_CLOB}/midpoint", params={"token_id": "100"}).mock(
            return_value=httpx.Response(200, json={"mid_price": mid_yes}),
        )
    # Both share token ids 100 / 200 in this fixture so we just mock once per token id;
    # the second call will reuse the registered route. Spread is the last bid/ask we set.
    respx.get(f"{_CLOB}/midpoint", params={"token_id": "200"}).mock(
        return_value=httpx.Response(200, json={"mid_price": "0.5"}),
    )
    respx.get(f"{_CLOB}/book", params={"token_id": "100"}).mock(
        return_value=httpx.Response(
            200,
            json=_book_payload(token_id="100", midpoint="0.5", last_trade="0.5"),
        ),
    )
    respx.get(f"{_CLOB}/book", params={"token_id": "200"}).mock(
        return_value=httpx.Response(
            200,
            json=_book_payload(token_id="200", midpoint="0.5", last_trade="0.5"),
        ),
    )

    async with PolymarketGammaClient() as gamma, PolymarketClobClient() as clob:
        signals = await fetch_polymarket_signals(
            gamma_client=gamma,
            clob_client=clob,
            identifiers=["a", "b"],
        )

    assert len(signals) == 2
    assert signals[0].slug == "a"
    assert signals[1].slug == "b"


# === ObserverAgent.build_user_message ===


def _input_with_signal(*, midpoint: Decimal, label: str = "Yes") -> ObserverInput:
    signal = PolymarketSignal(
        market_id="12345",
        slug="fed-cuts-2026",
        question="Will the Fed cut in 2026?",
        end_date=datetime(2026, 12, 31, tzinfo=UTC),
        is_binary=True,
        outcomes=(
            PolymarketOutcome(label=label, token_id="100", midpoint=midpoint),
            PolymarketOutcome(label="No", token_id="200", midpoint=Decimal("1") - midpoint),
        ),
        fetched_at=datetime.now(UTC),
    )
    return ObserverInput(
        universe=("AAPL",),
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        horizon_days=30,
        dimensions=(DataDimension.PRICES, DataDimension.MACRO),
        polymarket_signals=(signal,),
    )


def test_observer_user_message_includes_polymarket_block() -> None:
    agent = ObserverAgent.__new__(ObserverAgent)  # avoid provider construction
    payload = _input_with_signal(midpoint=Decimal("0.62"))
    msg = agent.build_user_message(payload)
    assert "Polymarket implied probabilities" in msg
    assert "P(Yes)=0.62" in msg
    assert "Will the Fed cut in 2026?" in msg


def test_observer_user_message_skips_block_when_no_signals() -> None:
    agent = ObserverAgent.__new__(ObserverAgent)
    payload = ObserverInput(
        universe=("AAPL",),
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        horizon_days=30,
        dimensions=(DataDimension.PRICES,),
    )
    msg = agent.build_user_message(payload)
    assert "Polymarket" not in msg


def test_observer_user_message_handles_multi_outcome_signal() -> None:
    agent = ObserverAgent.__new__(ObserverAgent)
    multi = PolymarketSignal(
        market_id="999",
        slug="election",
        question="Who wins?",
        end_date=datetime(2026, 11, 5, tzinfo=UTC),
        is_binary=False,
        outcomes=(
            PolymarketOutcome(label="A", token_id="1", midpoint=Decimal("0.45")),
            PolymarketOutcome(label="B", token_id="2", midpoint=Decimal("0.30")),
            PolymarketOutcome(label="C", token_id="3", midpoint=None),
        ),
        fetched_at=datetime.now(UTC),
    )
    payload = ObserverInput(
        universe=("AAPL",),
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        horizon_days=30,
        dimensions=(DataDimension.MACRO,),
        polymarket_signals=(multi,),
    )
    msg = agent.build_user_message(payload)
    assert "A=0.45" in msg
    assert "B=0.30" in msg
    assert "C=?" in msg


def test_observer_user_message_falls_through_when_yes_midpoint_missing() -> None:
    """Binary market with no Yes midpoint falls back to per-outcome listing."""
    agent = ObserverAgent.__new__(ObserverAgent)
    sig = PolymarketSignal(
        market_id="42",
        slug="x",
        question="?",
        end_date=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=30),
        is_binary=True,
        outcomes=(
            PolymarketOutcome(label="Yes", token_id="100", midpoint=None),
            PolymarketOutcome(label="No", token_id="200", midpoint=Decimal("0.55")),
        ),
        fetched_at=datetime.now(UTC),
    )
    payload = ObserverInput(
        universe=("AAPL",),
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        horizon_days=30,
        dimensions=(DataDimension.MACRO,),
        polymarket_signals=(sig,),
    )
    msg = agent.build_user_message(payload)
    assert "Yes=?" in msg
    assert "No=0.55" in msg
    assert "P(Yes)=" not in msg
