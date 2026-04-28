"""Policy Gateway projection: ``Π : StrategyDecision → FeasibleAction``.

Implementation note on "demote vs filter":
We **never** silently drop offending targets and let the rest through.
A decision with even one violation is demoted whole to ``defer`` with
``targets=()``, because partially executing an agent's intended portfolio
silently distorts its risk profile (the rejected leg may be the hedge).
The agent — or a downstream supervisor — gets the full violation list
and can re-emit a corrected decision.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from caqrs.schemas.common import StrictBaseModel, Ticker
from caqrs.schemas.decision import (
    DecisionAction,
    StrategyDecision,
    TargetPosition,
)


class PolicyViolationKind(StrEnum):
    """Enumerated violation kinds. Adding a new constraint adds one
    member here and one branch in :func:`apply_policy_gateway`."""

    NOTIONAL_CAP_EXCEEDED = "notional_cap_exceeded"
    TICKER_DENY_LISTED = "ticker_deny_listed"
    TICKER_NOT_ALLOWED = "ticker_not_allowed"


class PolicyViolation(StrictBaseModel):
    """One projected constraint failure. ``context`` is a free-form
    ``str→str`` map kept narrow so violations are JSON-serialisable
    without bespoke encoders."""

    kind: PolicyViolationKind
    message: str = Field(min_length=1, max_length=400)
    context: dict[str, str] = Field(default_factory=dict)


class PolicyGatewayConfig(StrictBaseModel):
    """Caller-supplied account-level constraints.

    All fields are optional; the empty config is the identity projection
    (every ADOPT decision passes through unchanged).
    """

    account_notional_cap_usd: Decimal | None = Field(default=None, ge=0)
    allowed_tickers: tuple[Ticker, ...] | None = None
    denied_tickers: tuple[Ticker, ...] = ()

    @model_validator(mode="after")
    def _no_allow_deny_overlap(self) -> Self:
        if self.allowed_tickers is None:
            return self
        overlap = set(self.allowed_tickers) & set(self.denied_tickers)
        if overlap:
            raise ValueError(
                f"allowed_tickers and denied_tickers overlap: {sorted(overlap)}",
            )
        return self


class FeasibleAction(StrictBaseModel):
    """Projected output of the gateway.

    Carries the (possibly demoted) action, the (possibly emptied) target
    list, and the full set of violations encountered. Callers route this
    to a broker only when ``action == ADOPT and not violations``.
    """

    action: DecisionAction
    targets: tuple[TargetPosition, ...] = ()
    violations: tuple[PolicyViolation, ...] = ()
    source_decision_run_id: str

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        if self.action == DecisionAction.ADOPT and not self.targets:
            raise ValueError("action=adopt requires at least one target position.")
        if self.action != DecisionAction.ADOPT and self.targets:
            raise ValueError("targets must be empty unless action=adopt.")
        if self.action == DecisionAction.ADOPT and self.violations:
            raise ValueError(
                "action=adopt with violations is contradictory; "
                "a violating decision must be demoted to defer.",
            )
        return self


def _collect_violations(
    *,
    decision: StrategyDecision,
    config: PolicyGatewayConfig,
) -> tuple[PolicyViolation, ...]:
    violations: list[PolicyViolation] = []

    if (
        config.account_notional_cap_usd is not None
        and decision.notional_cap_usd > config.account_notional_cap_usd
    ):
        violations.append(
            PolicyViolation(
                kind=PolicyViolationKind.NOTIONAL_CAP_EXCEEDED,
                message=(
                    f"decision notional_cap_usd {decision.notional_cap_usd} exceeds "
                    f"account cap {config.account_notional_cap_usd}"
                ),
                context={
                    "decision_notional": str(decision.notional_cap_usd),
                    "account_cap": str(config.account_notional_cap_usd),
                },
            ),
        )

    denied = set(config.denied_tickers)
    allowed = set(config.allowed_tickers) if config.allowed_tickers is not None else None
    for target in decision.targets:
        if target.ticker in denied:
            violations.append(
                PolicyViolation(
                    kind=PolicyViolationKind.TICKER_DENY_LISTED,
                    message=f"ticker {target.ticker} is on the deny list",
                    context={"ticker": target.ticker},
                ),
            )
        if allowed is not None and target.ticker not in allowed:
            violations.append(
                PolicyViolation(
                    kind=PolicyViolationKind.TICKER_NOT_ALLOWED,
                    message=f"ticker {target.ticker} is not in the allow list",
                    context={"ticker": target.ticker},
                ),
            )

    return tuple(violations)


def apply_policy_gateway(
    *,
    decision: StrategyDecision,
    config: PolicyGatewayConfig,
) -> FeasibleAction:
    """Project ``decision`` against ``config``.

    Pass-through for non-ADOPT decisions (REJECT / DEFER are already
    no-trade outcomes; the gateway has no projection work). For ADOPT,
    collect every violation in one pass; if any is found, demote to
    DEFER with ``targets=()`` and the full violation tuple attached.
    """
    if decision.action != DecisionAction.ADOPT:
        return FeasibleAction(
            action=decision.action,
            targets=(),
            violations=(),
            source_decision_run_id=decision.metadata.run_id,
        )

    violations = _collect_violations(decision=decision, config=config)
    if violations:
        return FeasibleAction(
            action=DecisionAction.DEFER,
            targets=(),
            violations=violations,
            source_decision_run_id=decision.metadata.run_id,
        )
    return FeasibleAction(
        action=DecisionAction.ADOPT,
        targets=decision.targets,
        violations=(),
        source_decision_run_id=decision.metadata.run_id,
    )
