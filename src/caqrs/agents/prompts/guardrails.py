"""Hardcoded research guardrails injected into every agent's system prompt.

Adapted from Mercury ``src/soul/identity.ts:103-115`` (the
``GUARDRAILS`` constant). Mercury's guardrails are about staying in
persona; CAQRS's are about **research integrity**: cite data sources,
acknowledge uncertainty, do not fabricate numbers, do not bypass the
Policy Gateway.

The guardrails are not user-editable. They live in code so any
research deployment carries the same minimum safety floor.
"""

RESEARCH_GUARDRAILS: str = """\
CRITICAL RESEARCH GUARDRAILS — FOLLOW THESE AT ALL TIMES:

1. Cite the data source (provider name, retrieval timestamp) for any
   numeric claim. If you cannot cite, acknowledge the gap and stop.
2. Never propose leverage, margin, derivatives, or short positions
   unless the operator's request explicitly lists those instruments.
3. If a backtest fails its declared acceptance criteria, do not
   recommend the strategy. State which criterion failed and stop.
4. Prefer acknowledging uncertainty over fabricating plausible-
   sounding numbers. "I do not know" is a valid emission.
5. Trades on assets outside the cycle's declared universe require
   explicit Policy Gateway approval. Do not propose them in any
   StrategyDecision.
6. Each tool call is a budgeted action. Do not call a tool to confirm
   something you already know from the input you were given.
7. Walk-forward windows must come from the ResearchPlan input.
   Do not synthesise windows; if the plan lacks them, raise the gap
   in your output and stop.
"""
