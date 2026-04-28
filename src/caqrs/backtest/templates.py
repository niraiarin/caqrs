"""Strategy template registry — discriminated-union over the built-in
backtest signal generators.

Why a discriminated union instead of free-form kwargs?

- :class:`ResearchPlan` is the agent-visible JSON contract. Embedding a
  :class:`StrategySpec` in it (eventually) lets the LLM choose between
  buy-and-hold / momentum / mean-reversion by emitting a single
  ``template`` discriminator + the associated parameters; Pydantic
  validates the shape, no string parsing required.
- :func:`make_jquants_executor` is the single dispatch entry point so
  callers (CycleRunner, smoke scripts, eventual policy gateway) never
  branch on ``template`` themselves.

This module is the only place that knows the mapping between
``template`` strings and concrete factories. Adding a new template means
adding one ``…Spec`` class and one branch in :func:`make_jquants_executor`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal, assert_never

from pydantic import BaseModel, ConfigDict, Field

from caqrs.backtest.providers import (
    BacktestExecutor,
    make_jquants_buy_and_hold_executor,
    make_jquants_mean_reversion_executor,
    make_jquants_momentum_executor,
)
from caqrs.data.jquants import JQuantsClient

_DEFAULT_NOTIONAL_USD = Decimal("1000000")


class _StrategySpecBase(BaseModel):
    """Strict base for every strategy spec: frozen + extra=forbid so a
    typo in the JSON contract is a hard ValidationError, not a silent
    no-op."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class BuyAndHoldSpec(_StrategySpecBase):
    """Equal-weight buy-and-hold over ``plan.universe``. Baseline."""

    template: Literal["buy_and_hold"] = "buy_and_hold"


class MomentumSpec(_StrategySpecBase):
    """Top-K momentum: rank by past return over ``lookback_days``,
    long-equal-weight the top ``top_k`` tickers. ``top_k=None`` →
    rebalance equal-weight across the whole universe."""

    template: Literal["momentum"] = "momentum"
    lookback_days: int = Field(gt=0)
    top_k: int | None = Field(default=None, ge=1)


class MeanReversionSpec(_StrategySpecBase):
    """Bottom-K mean reversion: rank by past return over
    ``lookback_days``, long-equal-weight the worst ``bottom_k`` tickers.
    ``bottom_k=None`` → rebalance equal-weight across the whole universe."""

    template: Literal["mean_reversion"] = "mean_reversion"
    lookback_days: int = Field(gt=0)
    bottom_k: int | None = Field(default=None, ge=1)


StrategySpec = Annotated[
    BuyAndHoldSpec | MomentumSpec | MeanReversionSpec,
    Field(discriminator="template"),
]


def make_jquants_executor(
    *,
    spec: BuyAndHoldSpec | MomentumSpec | MeanReversionSpec,
    client: JQuantsClient,
    notional_usd: Decimal = _DEFAULT_NOTIONAL_USD,
) -> BacktestExecutor:
    """Dispatch a :class:`StrategySpec` to the matching J-Quants executor.

    Returns a :data:`BacktestExecutor` ready to plug into
    :class:`CycleRunner`. Adding a template means adding a branch here
    and the corresponding ``…Spec`` class above.
    """
    if isinstance(spec, BuyAndHoldSpec):
        return make_jquants_buy_and_hold_executor(
            client=client,
            notional_usd=notional_usd,
        )
    if isinstance(spec, MomentumSpec):
        return make_jquants_momentum_executor(
            client=client,
            lookback_days=spec.lookback_days,
            top_k=spec.top_k,
            notional_usd=notional_usd,
        )
    if isinstance(spec, MeanReversionSpec):
        return make_jquants_mean_reversion_executor(
            client=client,
            lookback_days=spec.lookback_days,
            bottom_k=spec.bottom_k,
            notional_usd=notional_usd,
        )
    assert_never(spec)
