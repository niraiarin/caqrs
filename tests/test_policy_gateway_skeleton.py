"""P3.a — Policy Gateway projection: schema + pure function.

The gateway sits between :class:`StrategyDecision` (agent emit point) and any
broker adapter. It applies caller-supplied **account-level caps** the agent
doesn't know about (total notional already deployed, deny-listed tickers,
…) and projects the decision into a :class:`FeasibleAction` — either the
original decision unchanged, or ``defer`` with a typed violation report.

The pure-function shape (no I/O, no broker SDK) is deliberate: the gateway
is auditable, deterministic, and trivially testable. Subsequent P3 slices
layer in additional constraints (loss-budget consumption, position
aggregation, lot rounding) by extending :class:`PolicyGatewayConfig` and
adding violation kinds, never by touching the function signature.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from caqrs.policy.gateway import (
    FeasibleAction,
    PolicyGatewayConfig,
    PolicyViolation,
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


def _adopt_decision(
    *,
    notional_cap_usd: Decimal = Decimal("1000000"),
    targets: tuple[TargetPosition, ...] = (
        TargetPosition(ticker="AAPL", side=Side.BUY, weight=Decimal("0.6")),
        TargetPosition(ticker="MSFT", side=Side.BUY, weight=Decimal("0.4")),
    ),
) -> StrategyDecision:
    return StrategyDecision(
        metadata=_meta(),
        backtest_run_id=new_run_id(),
        action=DecisionAction.ADOPT,
        targets=targets,
        rationale="test",
        notional_cap_usd=notional_cap_usd,
        max_position_weight=Decimal("0.7"),
        daily_loss_limit_usd=Decimal("10000"),
    )


# === Schema construction ===


def test_policy_violation_construction() -> None:
    v = PolicyViolation(
        kind=PolicyViolationKind.NOTIONAL_CAP_EXCEEDED,
        message="decision notional 2000000 exceeds account cap 500000",
        context={"decision_notional": "2000000", "account_cap": "500000"},
    )
    assert v.kind is PolicyViolationKind.NOTIONAL_CAP_EXCEEDED
    assert "exceeds" in v.message


def test_policy_violation_is_frozen_extra_forbid() -> None:
    v = PolicyViolation(
        kind=PolicyViolationKind.TICKER_DENY_LISTED,
        message="msg",
        context={},
    )
    with pytest.raises(ValidationError, match="frozen"):
        v.message = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        PolicyViolation(
            kind=PolicyViolationKind.TICKER_DENY_LISTED,
            message="msg",
            context={},
            extra="x",  # type: ignore[call-arg]
        )


def test_policy_gateway_config_defaults() -> None:
    cfg = PolicyGatewayConfig()
    assert cfg.account_notional_cap_usd is None
    assert cfg.allowed_tickers is None
    assert cfg.denied_tickers == ()


def test_policy_gateway_config_rejects_overlap_allow_deny() -> None:
    with pytest.raises(ValidationError, match="overlap"):
        PolicyGatewayConfig(
            allowed_tickers=("AAPL", "MSFT"),
            denied_tickers=("MSFT",),
        )


# === FeasibleAction ===


def test_feasible_action_carries_decision_and_no_violations_when_clean() -> None:
    decision = _adopt_decision()
    fa = apply_policy_gateway(decision=decision, config=PolicyGatewayConfig())
    assert isinstance(fa, FeasibleAction)
    assert fa.violations == ()
    assert fa.action is DecisionAction.ADOPT
    # Targets carried through verbatim when no projection is applied.
    assert fa.targets == decision.targets


def test_feasible_action_round_trips_through_json() -> None:
    decision = _adopt_decision()
    fa = apply_policy_gateway(decision=decision, config=PolicyGatewayConfig())
    payload = fa.model_dump_json()
    restored = FeasibleAction.model_validate_json(payload)
    assert restored == fa


# === Notional-cap projection ===


def test_notional_cap_below_decision_demotes_to_defer() -> None:
    decision = _adopt_decision(notional_cap_usd=Decimal("2000000"))
    cfg = PolicyGatewayConfig(account_notional_cap_usd=Decimal("500000"))
    fa = apply_policy_gateway(decision=decision, config=cfg)
    assert fa.action is DecisionAction.DEFER
    assert fa.targets == ()
    kinds = {v.kind for v in fa.violations}
    assert PolicyViolationKind.NOTIONAL_CAP_EXCEEDED in kinds


def test_notional_cap_at_or_above_decision_passes() -> None:
    decision = _adopt_decision(notional_cap_usd=Decimal("500000"))
    cfg = PolicyGatewayConfig(account_notional_cap_usd=Decimal("500000"))
    fa = apply_policy_gateway(decision=decision, config=cfg)
    assert fa.action is DecisionAction.ADOPT
    assert fa.violations == ()


# === Allow / deny list projection ===


def test_denied_ticker_demotes_to_defer() -> None:
    decision = _adopt_decision()
    cfg = PolicyGatewayConfig(denied_tickers=("MSFT",))
    fa = apply_policy_gateway(decision=decision, config=cfg)
    assert fa.action is DecisionAction.DEFER
    assert fa.targets == ()
    kinds = {v.kind for v in fa.violations}
    assert PolicyViolationKind.TICKER_DENY_LISTED in kinds
    # Violation context names the offending ticker.
    msft_violation = next(
        v for v in fa.violations if v.kind is PolicyViolationKind.TICKER_DENY_LISTED
    )
    assert msft_violation.context.get("ticker") == "MSFT"


def test_allow_list_excludes_unlisted_ticker() -> None:
    decision = _adopt_decision()
    cfg = PolicyGatewayConfig(allowed_tickers=("AAPL",))
    fa = apply_policy_gateway(decision=decision, config=cfg)
    assert fa.action is DecisionAction.DEFER
    kinds = {v.kind for v in fa.violations}
    assert PolicyViolationKind.TICKER_NOT_ALLOWED in kinds


def test_allow_list_with_all_tickers_passes() -> None:
    decision = _adopt_decision()
    cfg = PolicyGatewayConfig(allowed_tickers=("AAPL", "MSFT"))
    fa = apply_policy_gateway(decision=decision, config=cfg)
    assert fa.action is DecisionAction.ADOPT
    assert fa.violations == ()


# === Action passthrough ===


def test_reject_decision_passes_through_unchanged() -> None:
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
    cfg = PolicyGatewayConfig(account_notional_cap_usd=Decimal("100"))
    fa = apply_policy_gateway(decision=decision, config=cfg)
    # Reject is already a no-trade outcome; the gateway has no projection
    # work and emits no violations.
    assert fa.action is DecisionAction.REJECT
    assert fa.targets == ()
    assert fa.violations == ()


def test_defer_decision_passes_through_unchanged() -> None:
    decision = StrategyDecision(
        metadata=_meta(),
        backtest_run_id=new_run_id(),
        action=DecisionAction.DEFER,
        targets=(),
        rationale="not enough confidence yet",
        notional_cap_usd=Decimal("1000000"),
        max_position_weight=Decimal("0.7"),
        daily_loss_limit_usd=Decimal("10000"),
    )
    cfg = PolicyGatewayConfig(denied_tickers=("AAPL",))
    fa = apply_policy_gateway(decision=decision, config=cfg)
    assert fa.action is DecisionAction.DEFER
    assert fa.targets == ()
    assert fa.violations == ()


# === Multiple violations aggregate ===


def test_multiple_violations_are_all_reported() -> None:
    """Gateway reports every violation it finds in one pass — never short-
    circuits — so callers can show the agent the full list."""
    decision = _adopt_decision(notional_cap_usd=Decimal("2000000"))
    cfg = PolicyGatewayConfig(
        account_notional_cap_usd=Decimal("500000"),
        denied_tickers=("AAPL", "MSFT"),
    )
    fa = apply_policy_gateway(decision=decision, config=cfg)
    kinds = [v.kind for v in fa.violations]
    assert kinds.count(PolicyViolationKind.NOTIONAL_CAP_EXCEEDED) == 1
    assert kinds.count(PolicyViolationKind.TICKER_DENY_LISTED) == 2
