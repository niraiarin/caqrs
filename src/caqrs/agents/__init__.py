"""Agent protocols, base classes, and concrete agents.

P1.0 shipped the ``Agent[I, O]`` Protocol + ``AgentResult[T]`` wrapper.
P1.2.b added per-agent prompt assembly (`prompts/`).
P1.2.c adds ``LLMAgent`` — convenience base for LLM-backed agents that
wraps provider + prompt + structured-output run.
P1.2.d will add the concrete agents (Observer, Hypothesis, Skeptic,
Research, Auditor) on top of ``LLMAgent``.
"""

from caqrs.agents.base_llm_agent import LLMAgent
from caqrs.agents.protocol import Agent, AgentResult

__all__ = ["Agent", "AgentResult", "LLMAgent"]
