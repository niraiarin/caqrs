"""Provider protocol shape and stub behaviour tests."""

import asyncio
import json
from pathlib import Path

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


def test_codex_cli_stub_raises_not_implemented() -> None:
    p = OpenAIViaCodexCLI()
    with pytest.raises(NotImplementedError, match=r"P1\.1\.c"):
        asyncio.run(
            p.complete(
                messages=(Message(role=Role.USER, content="hi"),),
                schema=_Out,
                max_output_tokens=64,
            ),
        )


# AnthropicViaClaudeCLI is fully implemented as of P1.1.c.1; its behaviour
# (HTTP transport, OAuth-vs-API-key auth, tool-use parsing, error mapping)
# is covered by tests/test_anthropic_cli.py.
# OpenAICompatProvider is fully implemented as of P1.1.5; its behaviour
# is covered by tests/test_openai_compat.py.


# === is_configured() ===


def test_anthropic_is_configured_false_with_no_creds(tmp_path: Path) -> None:
    """The autouse Keychain-disable fixture means an empty home returns no creds."""
    p = AnthropicViaClaudeCLI(home_dir=tmp_path)
    assert p.is_configured() is False


def test_anthropic_is_configured_true_with_file_creds(tmp_path: Path) -> None:
    creds_dir = tmp_path / ".claude"
    creds_dir.mkdir()
    (creds_dir / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "a",
                    "refreshToken": "r",
                    "expiresAt": 1_900_000_000_000,
                },
            },
        ),
        encoding="utf-8",
    )
    p = AnthropicViaClaudeCLI(home_dir=tmp_path)
    assert p.is_configured() is True


def test_codex_is_configured_false_with_no_creds(tmp_path: Path) -> None:
    p = OpenAIViaCodexCLI(codex_home=tmp_path / ".codex")
    assert p.is_configured() is False


def test_codex_is_configured_true_with_file_creds(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(
        json.dumps(
            {"tokens": {"access_token": "a", "refresh_token": "r"}},
        ),
        encoding="utf-8",
    )
    p = OpenAIViaCodexCLI(codex_home=codex_home)
    assert p.is_configured() is True
