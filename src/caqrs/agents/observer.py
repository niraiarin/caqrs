"""ObserverAgent — entry point of a research cycle.

Receives an :class:`ObserverInput` (universe + horizon + requested data
dimensions) and emits an :class:`ObserverArtifact` summarising the
current market regime, per-asset metrics, news themes, and macro
signals.

The agent does not fetch raw data itself — that is delegated to the
data tools the orchestrator wires in (P2). At P1.2 the agent runs in
"summarise from a pre-prepared prompt" mode; the orchestrator is
expected to inject the raw data into the user message before invoking
``run``.

P1.6.c — when ``ObserverInput.polymarket_signals`` is populated the
default ``build_user_message`` formats the implied probabilities into
the prompt as a structured block so the LLM can fold prediction-market
sentiment into ``regime_summary`` / ``macro_notes``.
"""

from caqrs.agents.base_llm_agent import LLMAgent
from caqrs.agents.prompts import format_polymarket_block
from caqrs.schemas.observer import ObserverArtifact, ObserverInput


class ObserverAgent(LLMAgent[ObserverInput, ObserverArtifact]):
    name = "observer"
    role = "observer"
    role_brief = (
        "Observe the current market state for the requested universe and "
        "horizon. Summarise the regime, per-asset metrics, news themes, "
        "macro indicators, and any data quality concerns. When prediction-"
        "market signals are provided (Polymarket implied probabilities) "
        "fold them into the regime summary or macro notes."
    )
    emit_tool_description = (
        "Emit a single ObserverArtifact: regime_summary (1-3 sentences), "
        "asset_snapshots (one per ticker in the universe with available "
        "metrics), news_themes (≤20 short phrases), macro_notes (free "
        "text), data_quality_notes (one short note per concern), and "
        "polymarket_signals copied verbatim from the input when present."
    )
    input_schema = ObserverInput
    output_schema = ObserverArtifact

    def build_user_message(self, payload: ObserverInput) -> str:
        base = payload.model_dump_json(indent=2)
        block = format_polymarket_block(payload.polymarket_signals)
        return f"{base}\n\n{block}" if block else base
