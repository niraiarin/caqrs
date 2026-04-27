"""Tests for agent system-prompt assembly."""

import pytest

from caqrs.agents.prompts import RESEARCH_GUARDRAILS, build_agent_system_prompt

# === GUARDRAILS constant ===


def test_guardrails_has_research_specific_clauses() -> None:
    text = RESEARCH_GUARDRAILS.lower()
    assert "cite" in text
    assert "leverage" in text
    assert "policy gateway" in text
    assert "walk-forward" in text


def test_guardrails_is_concise() -> None:
    # ~150 tokens budget; characters as a rough proxy (~4 chars/token)
    assert len(RESEARCH_GUARDRAILS) < 1200


# === build_agent_system_prompt ===


def test_build_assembles_role_guardrails_and_emit_tool() -> None:
    prompt = build_agent_system_prompt(
        role="hypothesis",
        role_brief="Read the Observer artifact and emit a falsifiable hypothesis.",
        emit_tool_name="emit_HypothesisCard",
        emit_tool_description="The card must include claim, universe, and acceptance criteria.",
    )
    assert "hypothesis agent" in prompt
    assert "Read the Observer artifact" in prompt
    assert "emit_HypothesisCard" in prompt
    assert "claim, universe, and acceptance criteria" in prompt
    # GUARDRAILS injected verbatim
    assert "RESEARCH GUARDRAILS" in prompt
    assert "Cite the data source" in prompt


def test_build_mentions_emit_tool_name_at_least_twice() -> None:
    prompt = build_agent_system_prompt(
        role="skeptic",
        role_brief="Attempt to falsify the hypothesis.",
        emit_tool_name="emit_SkepticReport",
        emit_tool_description="Report any falsification path you find.",
    )
    # Once in the task line, once in the closing instruction
    assert prompt.count("emit_SkepticReport") >= 2


def test_build_strips_whitespace_from_inputs() -> None:
    prompt = build_agent_system_prompt(
        role="  observer  ",
        role_brief="\n  Survey the market state.\n  ",
        emit_tool_name="  emit_ObserverArtifact  ",
        emit_tool_description="\tEmit the observed regime.\t",
    )
    # leading/trailing whitespace stripped; content present
    assert "observer agent" in prompt
    assert "Survey the market state." in prompt
    assert "  emit_ObserverArtifact  " not in prompt
    assert "emit_ObserverArtifact" in prompt


def test_build_rejects_empty_inputs() -> None:
    base = {
        "role": "hypothesis",
        "role_brief": "Brief.",
        "emit_tool_name": "emit_X",
        "emit_tool_description": "Desc.",
    }
    for missing in base:
        kwargs = {**base, missing: "   "}
        with pytest.raises(ValueError, match=missing):
            build_agent_system_prompt(**kwargs)


def test_build_is_deterministic() -> None:
    args = {
        "role": "auditor",
        "role_brief": "Verify acceptance criteria pass.",
        "emit_tool_name": "emit_AuditReport",
        "emit_tool_description": "Pass-or-fail with one-line rationale.",
    }
    a = build_agent_system_prompt(**args)
    b = build_agent_system_prompt(**args)
    assert a == b
