"""Tests for the LLMAgent base class.

The tests substitute a fake ``LLMProvider`` so no real network or
subscription token is touched. Each test focuses on a single seam of
the base class (prompt assembly, payload validation, error mapping,
metadata propagation).
"""

from collections.abc import Sequence
from decimal import Decimal

import pytest
from pydantic import BaseModel, Field

from caqrs.agents import AgentResult, LLMAgent
from caqrs.providers import (
    AuthError,
    CompletionResult,
    Message,
    NetworkError,
    ProviderUsage,
)

# === Fake provider + fixture schemas ===


class _PulseInput(BaseModel):
    raw_bpm: int = Field(ge=0, le=300)


class _Pulse(BaseModel):
    """Heart-rate reading classified into a category."""

    bpm: int = Field(ge=0, le=300)
    category: str = Field(min_length=1)


class _RecordingProvider:
    provider_id: str = "fake-provider/test-model"

    def __init__(
        self,
        *,
        respond_with: BaseModel | None = None,
        raise_error: Exception | None = None,
        usage: ProviderUsage | None = None,
    ) -> None:
        self._respond_with = respond_with
        self._raise_error = raise_error
        self._usage = usage or ProviderUsage(
            token_in=12,
            token_out=8,
            latency_ms=42,
            cost_usd=Decimal(0),
        )
        self.received_messages: tuple[Message, ...] = ()
        self.received_schema: type[BaseModel] | None = None
        self.received_max_output_tokens: int = 0
        self.received_temperature: float = 0.0

    async def complete[T: BaseModel](
        self,
        *,
        messages: tuple[Message, ...],
        schema: type[T],
        max_output_tokens: int,
        temperature: float = 0.0,
    ) -> CompletionResult[T]:
        self.received_messages = messages
        self.received_schema = schema
        self.received_max_output_tokens = max_output_tokens
        self.received_temperature = temperature
        if self._raise_error is not None:
            raise self._raise_error
        if self._respond_with is None:
            raise AssertionError("provider configured with neither response nor error")
        return CompletionResult[T](
            output=self._respond_with,  # type: ignore[arg-type]
            usage=self._usage,
            provider_id=self.provider_id,
        )


class _PulseAgent(LLMAgent[_PulseInput, _Pulse]):
    name = "pulse"
    role = "pulse-classifier"
    role_brief = "Classify a raw BPM reading into a heart-rate category."
    emit_tool_description = "Emit a Pulse with bpm and category populated."
    input_schema = _PulseInput
    output_schema = _Pulse


def _ok_pulse() -> _Pulse:
    return _Pulse(bpm=72, category="resting")


# === Property tests ===


def test_emit_tool_name_derives_from_output_schema() -> None:
    provider = _RecordingProvider(respond_with=_ok_pulse())
    agent = _PulseAgent(provider=provider)
    assert agent.emit_tool_name == "emit__Pulse"  # private class → double underscore


def test_system_prompt_contains_role_and_emit_tool() -> None:
    provider = _RecordingProvider(respond_with=_ok_pulse())
    agent = _PulseAgent(provider=provider)
    prompt = agent.system_prompt
    assert "pulse-classifier agent" in prompt
    assert "Classify a raw BPM reading" in prompt
    assert "emit__Pulse" in prompt
    assert "RESEARCH GUARDRAILS" in prompt


def test_default_user_message_is_json_dump() -> None:
    provider = _RecordingProvider(respond_with=_ok_pulse())
    agent = _PulseAgent(provider=provider)
    payload = _PulseInput(raw_bpm=72)
    rendered = agent.build_user_message(payload)
    assert '"raw_bpm": 72' in rendered


# === run() success path ===


async def test_run_succeeds_and_propagates_usage(tmp_path_factory: pytest.TempPathFactory) -> None:
    del tmp_path_factory  # unused, but ensures async fixture wiring works
    output = _ok_pulse()
    usage = ProviderUsage(token_in=42, token_out=17, latency_ms=99, cost_usd=Decimal("0.001"))
    provider = _RecordingProvider(respond_with=output, usage=usage)
    agent = _PulseAgent(provider=provider)

    result = await agent.run(_PulseInput(raw_bpm=72))

    assert result.is_ok()
    assert result.output is output
    assert result.metadata.agent_name == "pulse"
    assert result.metadata.model_id == "fake-provider/test-model"
    assert result.metadata.token_in == 42
    assert result.metadata.token_out == 17
    assert result.metadata.latency_ms == 99
    assert result.metadata.llm_cost_usd == Decimal("0.001")


async def test_run_passes_messages_and_schema_to_provider() -> None:
    provider = _RecordingProvider(respond_with=_ok_pulse())
    agent = _PulseAgent(provider=provider)

    await agent.run(_PulseInput(raw_bpm=80))

    assert provider.received_schema is _Pulse
    assert _has_role(provider.received_messages, "system")
    assert _has_role(provider.received_messages, "user")
    user_msg = next(m for m in provider.received_messages if m.role.value == "user")
    assert '"raw_bpm": 80' in user_msg.content


async def test_run_uses_configured_max_tokens_and_temperature() -> None:
    provider = _RecordingProvider(respond_with=_ok_pulse())
    agent = _PulseAgent(provider=provider, max_output_tokens=512, temperature=0.7)

    await agent.run(_PulseInput(raw_bpm=80))

    assert provider.received_max_output_tokens == 512
    assert provider.received_temperature == pytest.approx(0.7)


async def test_run_records_parent_id_when_provided() -> None:
    provider = _RecordingProvider(respond_with=_ok_pulse())
    parent_id = "0123456789abcdef"
    agent = _PulseAgent(provider=provider, parent_run_id=parent_id)

    result = await agent.run(_PulseInput(raw_bpm=80))

    assert result.metadata.parent_id == parent_id


# === run() failure paths ===


async def test_run_wraps_provider_error_into_agent_result() -> None:
    provider = _RecordingProvider(raise_error=AuthError("token expired"))
    agent = _PulseAgent(provider=provider)

    result = await agent.run(_PulseInput(raw_bpm=80))

    assert not result.is_ok()
    assert result.output is None
    assert result.error is not None
    assert "AuthError" in result.error
    assert "token expired" in result.error
    # metadata should still be present, even on failure
    assert result.metadata.agent_name == "pulse"


async def test_run_wraps_network_error() -> None:
    provider = _RecordingProvider(raise_error=NetworkError("timeout"))
    agent = _PulseAgent(provider=provider)

    result = await agent.run(_PulseInput(raw_bpm=80))

    assert result.error is not None
    assert "NetworkError" in result.error


async def test_run_does_not_swallow_non_provider_exceptions() -> None:
    provider = _RecordingProvider(raise_error=RuntimeError("bug"))
    agent = _PulseAgent(provider=provider)

    with pytest.raises(RuntimeError, match="bug"):
        await agent.run(_PulseInput(raw_bpm=80))


# === input validation ===


async def test_run_rejects_wrong_input_type() -> None:
    provider = _RecordingProvider(respond_with=_ok_pulse())
    agent = _PulseAgent(provider=provider)

    class _Other(BaseModel):
        x: int

    with pytest.raises(TypeError, match="expected _PulseInput"):
        await agent.run(_Other(x=1))  # type: ignore[arg-type]


# === AgentResult shape ===


async def test_agent_result_is_frozen() -> None:
    provider = _RecordingProvider(respond_with=_ok_pulse())
    agent = _PulseAgent(provider=provider)

    result: AgentResult[_Pulse] = await agent.run(_PulseInput(raw_bpm=72))
    with pytest.raises(ValueError, match="frozen"):
        result.error = "tampered"  # type: ignore[misc]


# === helpers ===


def _has_role(messages: Sequence[Message], role: str) -> bool:
    return any(m.role.value == role for m in messages)
