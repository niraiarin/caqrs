"""HypothesisAgent — proposes a single falsifiable hypothesis.

Reads the :class:`ObserverArtifact` and emits one
:class:`HypothesisCard` with explicit acceptance criteria the Auditor
can later check against a backtest.

P1.6.e — when the Observer surfaced ``polymarket_signals`` the agent's
user message includes the same compact implied-probabilities block
the Observer prompt uses, so the rationale can ground in
prediction-market sentiment as well as price/news signals.
"""

from caqrs.agents.base_llm_agent import LLMAgent
from caqrs.agents.prompts import format_polymarket_block
from caqrs.schemas.hypothesis_card import HypothesisCard
from caqrs.schemas.observer import ObserverArtifact


class HypothesisAgent(LLMAgent[ObserverArtifact, HypothesisCard]):
    name = "hypothesis"
    role = "hypothesis"
    role_brief = (
        "Read the Observer artifact and propose a single falsifiable "
        "hypothesis. Constrain the universe to a subset that the "
        "observer's data covers; pick variables you can compute; "
        "specify acceptance criteria the backtest can later check. "
        "When the Observer surfaced Polymarket implied probabilities, "
        "consider whether the hypothesis is consistent with — or "
        "structurally bets against — the prediction-market consensus, "
        "and reflect that in the rationale."
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

    def build_user_message(self, payload: ObserverArtifact) -> str:
        base = payload.model_dump_json(indent=2)
        block = format_polymarket_block(payload.polymarket_signals)
        return f"{base}\n\n{block}" if block else base
