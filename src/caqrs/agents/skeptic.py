"""SkepticAgent — adversarial review of a hypothesis.

Attempts to falsify the :class:`HypothesisCard`. Emits a
:class:`SkepticReport` with a verdict (proceed / require_revision /
kill) plus falsification paths and concerns.

The orchestrator routes the cycle based on the verdict: ``PROCEED``
advances to Research; ``REQUIRE_REVISION`` returns to Hypothesis with
the report attached; ``KILL`` ends the cycle.
"""

from caqrs.agents.base_llm_agent import LLMAgent
from caqrs.schemas.hypothesis_card import HypothesisCard
from caqrs.schemas.skeptic import SkepticReport


class SkepticAgent(LLMAgent[HypothesisCard, SkepticReport]):
    name = "skeptic"
    role = "skeptic"
    role_brief = (
        "Adversarially review the hypothesis. Surface concrete "
        "falsification paths (each with severity and an evidence marker "
        "describing what the backtest would reveal). Note any data, "
        "regime, or implementation concerns. Decide whether the cycle "
        "should proceed."
    )
    emit_tool_description = (
        "Emit a single SkepticReport with: verdict, falsification_paths "
        "(severity ∈ {low, medium, high, fatal}; FATAL forces verdict=kill), "
        "concerns, and a one-paragraph summary. PROCEED requires no "
        "fatal path."
    )
    input_schema = HypothesisCard
    output_schema = SkepticReport
