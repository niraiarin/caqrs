"""Per-agent system prompt assembler.

Inverts Mercury's 4-file soul model (`src/soul/identity.ts`). Each
CAQRS agent has a **role** plus the shared `RESEARCH_GUARDRAILS` plus
an emit-tool description. Mercury's research file 06 documents the
inversion rationale: agents are typed pure functions, not personas.
"""

from typing import Final

from caqrs.agents.prompts.guardrails import RESEARCH_GUARDRAILS

_TEMPLATE: Final[str] = """\
You are the {role} agent in a quantitative-research orchestrator.

Role: {role_brief}

{guardrails}

Task: emit exactly one structured output by calling the
"{emit_tool_name}" tool. {emit_tool_description}

Do not emit free text. Do not call other tools unless this prompt
explicitly lists them. Call "{emit_tool_name}" exactly once and stop.
"""


def build_agent_system_prompt(
    *,
    role: str,
    role_brief: str,
    emit_tool_name: str,
    emit_tool_description: str,
) -> str:
    """Compose the per-agent system prompt.

    The output is deterministic given the inputs and contains the
    full ``RESEARCH_GUARDRAILS`` block. Empty inputs are rejected so
    agents are forced to provide meaningful self-description.
    """
    if not role.strip():
        raise ValueError("role must not be empty")
    if not role_brief.strip():
        raise ValueError("role_brief must not be empty")
    if not emit_tool_name.strip():
        raise ValueError("emit_tool_name must not be empty")
    if not emit_tool_description.strip():
        raise ValueError("emit_tool_description must not be empty")

    return _TEMPLATE.format(
        role=role.strip(),
        role_brief=role_brief.strip(),
        guardrails=RESEARCH_GUARDRAILS.strip(),
        emit_tool_name=emit_tool_name.strip(),
        emit_tool_description=emit_tool_description.strip(),
    )
