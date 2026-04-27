"""Compose Gamma + CLOB into Observer-ready PolymarketSignals.

The Gamma API knows what a market is (slug, question, outcome
labels, the parallel ``clobTokenIds`` array). The CLOB API knows the
current orderbook for each token. Neither alone is enough for the
Observer; together they yield a typed snapshot the LLM can reason
about.

The helper functions here do the deterministic part — fetch metadata
+ per-token midpoints/spreads/last-trade — and return a
:class:`PolymarketSignal`. Failures on individual tokens degrade
gracefully: a missing midpoint surfaces as ``None`` rather than
aborting the whole signal, because partial information is still
informative for the LLM.
"""

from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal

from caqrs.data.polymarket.clob_client import PolymarketClobClient, PolymarketError
from caqrs.data.polymarket.gamma_client import PolymarketGammaClient
from caqrs.data.polymarket.schemas import GammaMarket
from caqrs.schemas.observer import PolymarketOutcome, PolymarketSignal


async def fetch_polymarket_signal(
    *,
    gamma_client: PolymarketGammaClient,
    clob_client: PolymarketClobClient,
    identifier: str,
    clock: object | None = None,
) -> PolymarketSignal:
    """Fetch one market's metadata + current per-outcome pricing.

    ``identifier`` may be either a numeric Polymarket id or a slug;
    Gamma routes both through the same path. ``clock`` exists only to
    stay symmetric with other CAQRS modules that take an injectable
    clock (we currently use ``datetime.now(UTC)`` directly).
    """
    del clock  # reserved for symmetry; not yet used
    market = await gamma_client.get_market(identifier)
    return await _materialise_signal(market=market, clob_client=clob_client)


async def fetch_polymarket_signals(
    *,
    gamma_client: PolymarketGammaClient,
    clob_client: PolymarketClobClient,
    identifiers: Iterable[str],
) -> tuple[PolymarketSignal, ...]:
    """Resolve every identifier (id or slug) into a PolymarketSignal.

    Markets are fetched serially; Polymarket's public rate limits are
    generous (~1.5k req / 10s for market-data routes) so a handful of
    markets per Observer cycle does not need parallelisation. If a
    future caller passes hundreds of identifiers we will revisit.
    """
    signals = [
        await fetch_polymarket_signal(
            gamma_client=gamma_client,
            clob_client=clob_client,
            identifier=identifier,
        )
        for identifier in identifiers
    ]
    return tuple(signals)


async def _materialise_signal(
    *,
    market: GammaMarket,
    clob_client: PolymarketClobClient,
) -> PolymarketSignal:
    outcomes: list[PolymarketOutcome] = []
    parallel = list(zip(market.outcomes, market.clob_token_ids, strict=False))
    for label, token_id in parallel:
        outcomes.append(
            await _fetch_outcome(
                label=label,
                token_id=token_id,
                clob_client=clob_client,
            ),
        )

    if not outcomes:
        # Gamma exposes some markets without populated tokens (e.g.
        # awaiting CLOB deployment). Surface a single placeholder
        # outcome so the schema's min_length=1 invariant holds; the
        # LLM will see all-None pricing and can choose to ignore.
        outcomes.append(
            PolymarketOutcome(
                label="(no outcome metadata)",
                token_id="0",
                midpoint=None,
                last_trade_price=None,
                spread=None,
            ),
        )

    return PolymarketSignal(
        market_id=market.id,
        slug=market.slug,
        question=market.question,
        end_date=market.end_date,
        is_binary=market.is_binary and len(outcomes) == 2,  # noqa: PLR2004 — local arity
        outcomes=tuple(outcomes),
        fetched_at=datetime.now(UTC),
    )


async def _fetch_outcome(
    *,
    label: str,
    token_id: str,
    clob_client: PolymarketClobClient,
) -> PolymarketOutcome:
    midpoint = await _safe_midpoint(clob_client=clob_client, token_id=token_id)
    spread, last_trade = await _safe_book_metrics(
        clob_client=clob_client,
        token_id=token_id,
    )
    return PolymarketOutcome(
        label=label,
        token_id=token_id,
        midpoint=midpoint,
        last_trade_price=last_trade,
        spread=spread,
    )


async def _safe_midpoint(
    *,
    clob_client: PolymarketClobClient,
    token_id: str,
) -> Decimal | None:
    try:
        return await clob_client.get_midpoint(token_id=token_id)
    except PolymarketError:
        return None


async def _safe_book_metrics(
    *,
    clob_client: PolymarketClobClient,
    token_id: str,
) -> tuple[Decimal | None, Decimal | None]:
    try:
        book = await clob_client.get_orderbook(token_id=token_id)
    except PolymarketError:
        return None, None
    return book.spread, book.last_trade_price
