"""HypothesisAgent — proposes a single falsifiable hypothesis.

Reads the :class:`ObserverArtifact` and emits one
:class:`HypothesisCard` with explicit acceptance criteria the Auditor
can later check against a backtest.
"""

from caqrs.agents.base_llm_agent import LLMAgent
from caqrs.schemas.hypothesis_card import HypothesisCard
from caqrs.schemas.observer import ObserverArtifact


class HypothesisAgent(LLMAgent[ObserverArtifact, HypothesisCard]):
    name = "hypothesis"
    role = "hypothesis"
    role_brief = (
        "Read the Observer artifact and propose a single falsifiable "
        "hypothesis. Constrain the universe to a subset that the "
        "observer's data covers; pick variables you can compute; "
        "specify acceptance criteria the backtest can later check."
    )
    emit_tool_description = (
        "Emit a single HypothesisCard with: claim (one sentence), "
        "rationale, universe (⊆ observer's universe), direction, "
        "horizon_days, variables, acceptance criteria (each as "
        "metric_path + op + threshold), max_drawdown_pct, and the "
        "expected_window_start / expected_window_end."
    )
    input_schema = ObserverArtifact
    output_schema = HypothesisCard
