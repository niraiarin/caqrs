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
from caqrs.schemas.observer import ObserverArtifact, ObserverInput, PolymarketSignal


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
        if not payload.polymarket_signals:
            return base
        return f"{base}\n\n{_format_polymarket_block(payload.polymarket_signals)}"


def _format_polymarket_block(signals: tuple[PolymarketSignal, ...]) -> str:
    """Render Polymarket signals as a compact, model-friendly block.

    The structured JSON dump above already contains every field; this
    block flags the Polymarket section explicitly so the LLM does not
    treat the signals as stray prose, and pre-computes the
    "probability of YES" view for binary markets where it is the
    natural single-number framing.
    """
    lines: list[str] = ["Polymarket implied probabilities (live snapshot):"]
    for sig in signals:
        slug = sig.slug or sig.market_id
        question = sig.question or "(no question)"
        if sig.is_binary:
            yes = next((o for o in sig.outcomes if o.label.casefold() == "yes"), None)
            if yes is not None and yes.midpoint is not None:
                lines.append(f"- {slug}: P(Yes)={yes.midpoint:.2f} — {question}")
                continue
        # Multi-outcome or no-yes-midpoint: list each outcome
        parts = [
            f"{o.label}={o.midpoint:.2f}" if o.midpoint is not None else f"{o.label}=?"
            for o in sig.outcomes
        ]
        lines.append(f"- {slug}: " + ", ".join(parts) + f" — {question}")
    return "\n".join(lines)
