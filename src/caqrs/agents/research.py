"""ResearchAgent — translates a proceed-verdict hypothesis into a walk-forward plan.

Receives a composite :class:`ResearchInput` (the original
:class:`HypothesisCard` plus the :class:`SkepticReport` that approved
it) and emits a :class:`ResearchPlan`.

The agent does **not** execute the backtest. Plan execution is the
backtest engine's job (P2); the orchestrator wires
``ResearchPlan → BacktestReport`` after this agent returns.
"""

from pydantic import BaseModel, ConfigDict

from caqrs.agents.base_llm_agent import LLMAgent
from caqrs.schemas.hypothesis_card import HypothesisCard
from caqrs.schemas.research_plan import ResearchPlan
from caqrs.schemas.skeptic import SkepticReport


class ResearchInput(BaseModel):
    """Composite input to the ResearchAgent. Carries the hypothesis and
    its approving skeptic report so the agent can incorporate the
    skeptic's concerns into the plan (e.g., extra walk-forward folds
    around regime boundaries)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    hypothesis: HypothesisCard
    skeptic: SkepticReport


class ResearchAgent(LLMAgent[ResearchInput, ResearchPlan]):
    name = "research"
    role = "research"
    role_brief = (
        "Translate the proceed-verdict hypothesis into a walk-forward "
        "research plan. Define disjoint train/test folds, cost and "
        "slippage assumptions, and a deterministic seed. Where the "
        "skeptic raised concerns, address them through the choice of "
        "folds or universe."
    )
    emit_tool_description = (
        "Emit a single ResearchPlan with: universe (⊆ hypothesis "
        "universe), frequency, walk_forward windows (train_start < "
        "train_end ≤ test_start < test_end; non-overlapping test "
        "windows across folds), cost_model_bps, slippage_bps, seed."
    )
    input_schema = ResearchInput
    output_schema = ResearchPlan
