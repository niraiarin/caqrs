"""Agent protocols, base classes, and concrete agents.

P1.0 shipped the ``Agent[I, O]`` Protocol + ``AgentResult[T]`` wrapper.
P1.2.b added per-agent prompt assembly (`prompts/`).
P1.2.c adds ``LLMAgent`` — convenience base for LLM-backed agents.
P1.2.d-2 adds the five concrete agents that traverse a research
cycle: Observer, Hypothesis, Skeptic, Research, Auditor.
P1.2.d-3 adds the Decider — emits a typed StrategyDecision when
the audit passes.
"""

from caqrs.agents.auditor import AuditorAgent, AuditorInput
from caqrs.agents.base_llm_agent import LLMAgent
from caqrs.agents.decider import DeciderAgent, DeciderInput
from caqrs.agents.hypothesis import HypothesisAgent
from caqrs.agents.observer import ObserverAgent
from caqrs.agents.protocol import Agent, AgentResult
from caqrs.agents.research import ResearchAgent, ResearchInput
from caqrs.agents.skeptic import SkepticAgent

__all__ = [
    "Agent",
    "AgentResult",
    "AuditorAgent",
    "AuditorInput",
    "DeciderAgent",
    "DeciderInput",
    "HypothesisAgent",
    "LLMAgent",
    "ObserverAgent",
    "ResearchAgent",
    "ResearchInput",
    "SkepticAgent",
]
