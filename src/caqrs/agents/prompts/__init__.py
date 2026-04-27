"""Per-agent system prompt building blocks.

CAQRS agents are typed pure functions of their input. Each agent has a
**role** (1-3 sentences) plus the shared ``RESEARCH_GUARDRAILS``
constant plus a description of the emit tool. Total system prompt is
~150 tokens per agent — by design, much smaller than Mercury's
~500-token soul / persona / taste / heartbeat baseline (see ADR-0001
and ``docs/research/mercury-survey/06-identity-and-soul.md``).
"""

from caqrs.agents.prompts.guardrails import RESEARCH_GUARDRAILS
from caqrs.agents.prompts.role_template import build_agent_system_prompt

__all__ = ["RESEARCH_GUARDRAILS", "build_agent_system_prompt"]
