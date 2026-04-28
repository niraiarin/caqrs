"""Walk-forward backtest engine.

P2.a — :func:`run_walk_forward` consumes a :class:`ResearchPlan`
plus pre-computed price and signal :class:`polars.DataFrame` inputs
and returns a :class:`BacktestReport`. Pure function; no I/O. The
caller wires whichever data source (e.g. :mod:`caqrs.data.jquants`)
and signal logic suits the strategy.

Subsequent slices add convenience factories that compose the engine
with a price provider + signal provider into a one-call
:data:`BacktestExecutor` plug-in for :class:`CycleRunner`.

Polars is required for this subpackage (already pulled in via the
``archive`` extras / dev group).
"""

from caqrs.backtest.engine import run_walk_forward
from caqrs.backtest.providers import (
    BacktestExecutor,
    JQuantsPriceProvider,
    PriceProvider,
    buy_and_hold_signals,
    make_jquants_buy_and_hold_executor,
    make_jquants_mean_reversion_executor,
    make_jquants_momentum_executor,
    mean_reversion_signals,
    momentum_signals,
)

__all__ = [
    "BacktestExecutor",
    "JQuantsPriceProvider",
    "PriceProvider",
    "buy_and_hold_signals",
    "make_jquants_buy_and_hold_executor",
    "make_jquants_mean_reversion_executor",
    "make_jquants_momentum_executor",
    "mean_reversion_signals",
    "momentum_signals",
    "run_walk_forward",
]
