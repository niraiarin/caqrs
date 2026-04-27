"""AuditorAgent — checks backtest results against acceptance criteria.

Receives a composite :class:`AuditorInput` (the original
:class:`HypothesisCard` plus the :class:`BacktestReport` produced by
executing its :class:`ResearchPlan`) and emits an
:class:`AuditReport`.

The agent's job is mechanical: walk the hypothesis's
``acceptance`` criteria, look up the corresponding metric in the
backtest report, decide pass/fail per criterion, and aggregate to a
verdict. The validators on ``AuditReport`` ensure verdict-vs-results
consistency.
"""

from pydantic import BaseModel, ConfigDict

from caqrs.agents.base_llm_agent import LLMAgent
from caqrs.schemas.audit import AuditReport
from caqrs.schemas.backtest_report import BacktestReport
from caqrs.schemas.hypothesis_card import HypothesisCard


class AuditorInput(BaseModel):
    """Composite input to the AuditorAgent. Carries the hypothesis (with
    its acceptance criteria) and the backtest report that was produced
    by executing the matching ResearchPlan."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    hypothesis: HypothesisCard
    backtest: BacktestReport


class AuditorAgent(LLMAgent[AuditorInput, AuditReport]):
    name = "auditor"
    role = "auditor"
    role_brief = (
        "Check the backtest report against the hypothesis's acceptance "
        "criteria. For each criterion, look up the corresponding metric "
        "in the backtest aggregate or fold metrics, compare against the "
        "threshold using the declared op, and record the actual value."
    )
    emit_tool_description = (
        "Emit a single AuditReport with: verdict (PASS iff every check "
        "passed; FAIL iff at least one failed), checks (one per "
        "hypothesis acceptance criterion, with metric_path / op / "
        "threshold copied verbatim plus the actual value and pass flag), "
        "and a short rationale."
    )
    input_schema = AuditorInput
    output_schema = AuditReport
