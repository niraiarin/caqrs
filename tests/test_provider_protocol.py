"""Provider protocol shape and stub behaviour tests."""

import asyncio

import pytest
from pydantic import BaseModel

from caqrs.providers import (
    AnthropicViaClaudeCLI,
    LLMProvider,
    Message,
    OpenAICompatProvider,
    OpenAIViaCodexCLI,
    Role,
)


class _Out(BaseModel):
    answer: str


def test_anthropic_cli_satisfies_protocol() -> None:
    p: LLMProvider = AnthropicViaClaudeCLI(model="claude-opus-4-7")
    assert isinstance(p, LLMProvider)
    assert p.provider_id == "anthropic-cli/claude-opus-4-7"


def test_codex_cli_satisfies_protocol() -> None:
    p: LLMProvider = OpenAIViaCodexCLI(model="gpt-5.5-codex")
    assert isinstance(p, LLMProvider)
    assert p.provider_id == "codex-cli/gpt-5.5-codex"


def test_openai_compat_satisfies_protocol() -> None:
    p: LLMProvider = OpenAICompatProvider(
        base_url="http://localhost:11500",
        api_key="test-key",
        model="qwen3-coder",
    )
    assert isinstance(p, LLMProvider)
    assert p.provider_id == "openai-compat/qwen3-coder"


def test_openai_compat_validates_constructor() -> None:
    with pytest.raises(ValueError, match="base_url"):
        OpenAICompatProvider(base_url="", api_key="k", model="m")
    with pytest.raises(ValueError, match="api_key"):
        OpenAICompatProvider(base_url="http://x", api_key="", model="m")
    with pytest.raises(ValueError, match="model"):
        OpenAICompatProvider(base_url="http://x", api_key="k", model="")


def test_anthropic_cli_stub_raises_not_implemented() -> None:
    p = AnthropicViaClaudeCLI()
    with pytest.raises(NotImplementedError, match=r"P1\.1"):
        asyncio.run(
            p.complete(
                messages=(Message(role=Role.USER, content="hi"),),
                schema=_Out,
                max_output_tokens=64,
            ),
        )


def test_codex_cli_stub_raises_not_implemented() -> None:
    p = OpenAIViaCodexCLI()
    with pytest.raises(NotImplementedError, match=r"P1\.1"):
        asyncio.run(
            p.complete(
                messages=(Message(role=Role.USER, content="hi"),),
                schema=_Out,
                max_output_tokens=64,
            ),
        )


def test_openai_compat_stub_raises_not_implemented() -> None:
    p = OpenAICompatProvider(base_url="http://x", api_key="k", model="m")
    with pytest.raises(NotImplementedError, match=r"P1\.1\.5"):
        asyncio.run(
            p.complete(
                messages=(Message(role=Role.USER, content="hi"),),
                schema=_Out,
                max_output_tokens=64,
            ),
        )
