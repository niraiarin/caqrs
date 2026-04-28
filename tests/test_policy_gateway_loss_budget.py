"""P3.b — Loss-budget consumption.

Extends :class:`PolicyGatewayConfig` with a caller-supplied
``daily_realized_loss_usd``: the loss already accumulated today against
the decision's ``daily_loss_limit_usd``. When the remaining budget
(``daily_loss_limit_usd - daily_realized_loss_usd``) is non-positive,
the gateway demotes the decision to ``defer`` with a
``LOSS_BUDGET_EXHAUSTED`` violation.

The gateway never **computes** the realized loss — that's the caller's
job (e.g. a future ``LossBudgetTracker`` reading the paper-broker's
position state). The Gateway just consumes whatever the caller hands
it. Keeps the function pure and testable.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from caqrs.policy.gateway import (
    PolicyGatewayConfig,
    PolicyViolationKind,
    apply_policy_gateway,
)
from caqrs.schemas.common import RunMetadata, new_run_id
from caqrs.schemas.decision import (
    DecisionAction,
    Side,
    StrategyDecision,
    TargetPosition,
)


def _meta() -> RunMetadata:
    return RunMetadata(
        run_id=new_run_id(),
        parent_id=None,
        agent_name="decider",
        model_id="test",
        created_at=datetime.now(UTC),
        llm_cost_usd=Decimal(0),
        latency_ms=0,
        token_in=0,
        token_out=0,
    )


def _adopt_decision(*, daily_loss_limit_usd: Decimal = Decimal("10000")) -> StrategyDecision:
    return StrategyDecision(
        metadata=_meta(),
        backtest_run_id=new_run_id(),
        action=DecisionAction.ADOPT,
        targets=(TargetPosition(ticker="AAPL", side=Side.BUY, weight=Decimal("0.5")),),
        rationale="test",
        notional_cap_usd=Decimal("1000000"),
        max_position_weight=Decimal("0.7"),
        daily_loss_limit_usd=daily_loss_limit_usd,
    )


# === Config schema additions ===


def test_config_default_realized_loss_is_zero() -> None:
    cfg = PolicyGatewayConfig()
    assert cfg.daily_realized_loss_usd == Decimal(0)


def test_config_rejects_negative_realized_loss() -> None:
    """Realized loss is a magnitude (always non-negative). The sign is
    implicit; a negative value is a caller bug."""
    with pytest.raises(ValidationError):
        PolicyGatewayConfig(daily_realized_loss_usd=Decimal("-1"))


def test_config_accepts_zero_realized_loss() -> None:
    cfg = PolicyGatewayConfig(daily_realized_loss_usd=Decimal(0))
    assert cfg.daily_realized_loss_usd == Decimal(0)


# === Projection: passthrough cases ===


def test_zero_realized_loss_with_default_limit_passes() -> None:
    """Default config with no realized loss is the identity projection."""
    decision = _adopt_decision()
    fa = apply_policy_gateway(decision=decision, config=PolicyGatewayConfig())
    assert fa.action is DecisionAction.ADOPT
    assert fa.violations == ()


def test_realized_loss_below_limit_passes() -> None:
    decision = _adopt_decision(daily_loss_limit_usd=Decimal("10000"))
    cfg = PolicyGatewayConfig(daily_realized_loss_usd=Decimal("5000"))
    fa = apply_policy_gateway(decision=decision, config=cfg)
    assert fa.action is DecisionAction.ADOPT
    assert fa.violations == ()


# === Projection: demote cases ===


def test_realized_loss_equal_to_limit_demotes() -> None:
    """Equal means zero remaining budget — already at the kill switch."""
    decision = _adopt_decision(daily_loss_limit_usd=Decimal("10000"))
    cfg = PolicyGatewayConfig(daily_realized_loss_usd=Decimal("10000"))
    fa = apply_policy_gateway(decision=decision, config=cfg)
    assert fa.action is DecisionAction.DEFER
    assert fa.targets == ()
    kinds = {v.kind for v in fa.violations}
    assert PolicyViolationKind.LOSS_BUDGET_EXHAUSTED in kinds


def test_realized_loss_above_limit_demotes() -> None:
    """Already in the red against today's budget — block further trades."""
    decision = _adopt_decision(daily_loss_limit_usd=Decimal("10000"))
    cfg = PolicyGatewayConfig(daily_realized_loss_usd=Decimal("12000"))
    fa = apply_policy_gateway(decision=decision, config=cfg)
    assert fa.action is DecisionAction.DEFER
    kinds = {v.kind for v in fa.violations}
    assert PolicyViolationKind.LOSS_BUDGET_EXHAUSTED in kinds


def test_violation_context_carries_remaining_budget() -> None:
    """Operators auditing the cycle log need to see how far over budget."""
    decision = _adopt_decision(daily_loss_limit_usd=Decimal("10000"))
    cfg = PolicyGatewayConfig(daily_realized_loss_usd=Decimal("12000"))
    fa = apply_policy_gateway(decision=decision, config=cfg)
    v = next(v for v in fa.violations if v.kind is PolicyViolationKind.LOSS_BUDGET_EXHAUSTED)
    assert v.context.get("daily_loss_limit_usd") == "10000"
    assert v.context.get("daily_realized_loss_usd") == "12000"
    assert v.context.get("remaining_budget_usd") == "-2000"


# === Composition with existing violation kinds ===


def test_loss_budget_violation_aggregates_with_other_violations() -> None:
    """All violations reported in one pass — agent sees the full picture."""
    decision = _adopt_decision(daily_loss_limit_usd=Decimal("10000"))
    cfg = PolicyGatewayConfig(
        daily_realized_loss_usd=Decimal("12000"),
        denied_tickers=("AAPL",),
    )
    fa = apply_policy_gateway(decision=decision, config=cfg)
    kinds = [v.kind for v in fa.violations]
    assert PolicyViolationKind.LOSS_BUDGET_EXHAUSTED in kinds
    assert PolicyViolationKind.TICKER_DENY_LISTED in kinds


def test_loss_budget_does_not_apply_to_reject_or_defer() -> None:
    """Non-ADOPT decisions are no-trade outcomes already; the budget
    constraint is irrelevant — the gateway stays a pass-through."""
    decision = StrategyDecision(
        metadata=_meta(),
        backtest_run_id=new_run_id(),
        action=DecisionAction.REJECT,
        targets=(),
        rationale="audit failed",
        notional_cap_usd=Decimal("1000000"),
        max_position_weight=Decimal("0.7"),
        daily_loss_limit_usd=Decimal("10000"),
    )
    cfg = PolicyGatewayConfig(daily_realized_loss_usd=Decimal("999999"))
    fa = apply_policy_gateway(decision=decision, config=cfg)
    assert fa.action is DecisionAction.REJECT
    assert fa.violations == ()
