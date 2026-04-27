"""Compact prompt block rendering for Polymarket signals.

Used by both the Observer and Hypothesis agents so the same
prediction-market summary reaches every LLM that gets a chance to
reason about it. The structured ``PolymarketSignal`` is already in
the JSON dump of the agent's input; this block surfaces the
implied-probability view explicitly so the model does not treat the
signals as stray prose.
"""

from caqrs.schemas.observer import PolymarketSignal


def format_polymarket_block(signals: tuple[PolymarketSignal, ...]) -> str:
    """Render Polymarket signals as a compact, model-friendly block.

    Returns an empty string when ``signals`` is empty so the caller
    can unconditionally append.
    """
    if not signals:
        return ""

    lines: list[str] = ["Polymarket implied probabilities (live snapshot):"]
    for sig in signals:
        slug = sig.slug or sig.market_id
        question = sig.question or "(no question)"
        if sig.is_binary:
            yes = next((o for o in sig.outcomes if o.label.casefold() == "yes"), None)
            if yes is not None and yes.midpoint is not None:
                lines.append(f"- {slug}: P(Yes)={yes.midpoint:.2f} — {question}")
                continue
        # Multi-outcome or no Yes midpoint: list each outcome
        parts = [
            f"{o.label}={o.midpoint:.2f}" if o.midpoint is not None else f"{o.label}=?"
            for o in sig.outcomes
        ]
        lines.append(f"- {slug}: " + ", ".join(parts) + f" — {question}")
    return "\n".join(lines)
