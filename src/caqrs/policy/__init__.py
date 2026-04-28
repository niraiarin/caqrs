"""Policy Gateway — projects :class:`StrategyDecision` artifacts against
caller-supplied account-level constraints into a :class:`FeasibleAction`.

The gateway is a pure function. It performs no I/O, holds no broker
state, and depends on no SDK; the caller is responsible for assembling
the live :class:`PolicyGatewayConfig` from whatever account-state source
is in use (paper broker, live broker, in-memory simulator).

P3.a covers notional-cap and ticker allow/deny-list constraints. Later
slices add loss-budget consumption, position-aggregation projection, and
broker-specific lot-size rounding by extending
:class:`PolicyGatewayConfig` and adding :class:`PolicyViolationKind`
members; the function signature stays stable.
"""

from caqrs.policy.gateway import (
    FeasibleAction,
    PolicyGatewayConfig,
    PolicyViolation,
    PolicyViolationKind,
    apply_policy_gateway,
)
from caqrs.policy.loss_budget_tracker import (
    LossBudgetTracker,
    RealizedPnLSource,
)

__all__ = [
    "FeasibleAction",
    "LossBudgetTracker",
    "PolicyGatewayConfig",
    "PolicyViolation",
    "PolicyViolationKind",
    "RealizedPnLSource",
    "apply_policy_gateway",
]
