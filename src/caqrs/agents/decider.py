"""DeciderAgent — turns a passing audit into a typed StrategyDecision.

The Decider runs only when the Auditor has verdict ``PASS``. It
receives the original :class:`HypothesisCard`, the :class:`BacktestReport`
that produced the metrics, and the :class:`AuditReport` that
confirmed the acceptance criteria, and emits a :class:`StrategyDecision`
with one of three actions: ``ADOPT`` (stand up live targets),
``REJECT`` (fail-stop despite the passing audit — e.g., second-order
concerns the metrics did not capture), or ``DEFER`` (re-evaluate
later, no live targets yet).

The agent does not execute trades. P3 (Policy Gateway) projects the
decision through asset / position / loss-limit envelopes; this agent
only emits the decision artifact.
"""

from pydantic import BaseModel, ConfigDict

from caqrs.agents.base_llm_agent import LLMAgent
from caqrs.schemas.audit import AuditReport
from caqrs.schemas.backtest_report import BacktestReport
from caqrs.schemas.decision import StrategyDecision
from caqrs.schemas.hypothesis_card import HypothesisCard


class DeciderInput(BaseModel):
    """Composite input to the DeciderAgent.

    Carries the hypothesis (with acceptance criteria), the backtest
    report (with realized metrics), and the audit report (with the
    per-criterion pass/fail trace) so the agent can reason about
    second-order concerns the audit did not have to score.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    hypothesis: HypothesisCard
    backtest: BacktestReport
    audit: AuditReport


class DeciderAgent(LLMAgent[DeciderInput, StrategyDecision]):
    name = "decider"
    role = "decider"
    role_brief = (
        "Turn a passing audit into a typed strategy decision. Pick "
        "ADOPT (stand up live targets), REJECT (despite a passing "
        "audit, second-order concerns block live deployment), or "
        "DEFER (re-evaluate later). For ADOPT, populate target "
        "positions whose weights satisfy max_position_weight and "
        "sum to <= 1."
    )
    emit_tool_description = (
        "Emit a single StrategyDecision: action ∈ {adopt, reject, defer}, "
        "targets (required iff adopt; each within max_position_weight; "
        "weights sum to <= 1), rationale (cite metrics / audit checks), "
        "and the policy envelopes notional_cap_usd / max_position_weight "
        "/ daily_loss_limit_usd."
    )
    input_schema = DeciderInput
    output_schema = StrategyDecision
