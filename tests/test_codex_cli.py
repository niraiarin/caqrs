"""Tests for OpenAIViaCodexCLI (Responses API at chatgpt.com/backend-api/codex)."""

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import BaseModel, Field

from caqrs.providers import (
    AuthError,
    Message,
    NetworkError,
    OpenAIViaCodexCLI,
    ProviderError,
    RateLimitError,
    Role,
)
from caqrs.providers.codex_cli import (
    _build_headers,
    _schema_to_tool_def,
)
from caqrs.providers.errors import SchemaViolationError


class _Sentiment(BaseModel):
    """Market sentiment classification."""

    label: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
_FUTURE_EXPIRY_MS = int((time.time() + 3600) * 1000)
_TOOL_NAME = "emit__Sentiment"


def _write_oauth_creds(
    home: Path, *, token: str = "oat-codex-test", account_id: str = "acct-1"
) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": token,
                    "refresh_token": "rt-test",
                    "account_id": account_id,
                    "id_token": "id-token",
                },
                "last_refresh": _FUTURE_EXPIRY_MS,
            },
        ),
        encoding="utf-8",
    )


def _ok_response(arguments: dict[str, Any], tool_name: str = _TOOL_NAME) -> dict[str, Any]:
    return {
        "id": "resp_test",
        "object": "response",
        "model": "gpt-5.5-codex",
        "output": [
            {
                "type": "function_call",
                "call_id": "call-1",
                "name": tool_name,
                "arguments": json.dumps(arguments),
            },
        ],
        "usage": {"input_tokens": 31, "output_tokens": 14},
    }


def _msgs() -> tuple[Message, ...]:
    return (Message(role=Role.USER, content="Classify SPY momentum."),)


# === Header selection ===


def test_headers_set_bearer_and_account_id() -> None:
    headers = _build_headers("token-abc", "acct-99")
    assert headers["Authorization"] == "Bearer token-abc"
    assert headers["ChatGPT-Account-Id"] == "acct-99"
    assert headers["content-type"] == "application/json"


def test_headers_omit_account_id_when_absent() -> None:
    headers = _build_headers("token-abc", None)
    assert "ChatGPT-Account-Id" not in headers
    assert headers["Authorization"] == "Bearer token-abc"


# === Tool definition (Responses API: flat, not nested) ===


def test_schema_to_tool_def_is_flat() -> None:
    tool = _schema_to_tool_def(_Sentiment)
    assert tool["type"] == "function"
    assert tool["name"] == _TOOL_NAME
    assert "function" not in tool  # not nested as in Chat Completions
    assert "parameters" in tool
    assert "$schema" not in tool["parameters"]


# === is_configured / pre-call gate ===


def test_is_configured_false_without_creds(tmp_path: Path) -> None:
    p = OpenAIViaCodexCLI(codex_home=tmp_path / ".codex")
    assert p.is_configured() is False


def test_is_configured_true_with_creds(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    _write_oauth_creds(home)
    p = OpenAIViaCodexCLI(codex_home=home)
    assert p.is_configured() is True


async def test_complete_raises_when_not_logged_in(tmp_path: Path) -> None:
    p = OpenAIViaCodexCLI(codex_home=tmp_path / ".codex")
    with pytest.raises(AuthError, match="not logged in"):
        await p.complete(messages=_msgs(), schema=_Sentiment, max_output_tokens=64)


@pytest.mark.traces("PROV-A4")
async def test_complete_raises_when_cred_expired(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    home.mkdir(parents=True)
    (home / "auth.json").write_text(
        json.dumps(
            {
                "tokens": {"access_token": "old-token", "refresh_token": "rt"},
                "last_refresh": 0,  # expiry derived as ~1h after epoch
            },
        ),
        encoding="utf-8",
    )
    p = OpenAIViaCodexCLI(codex_home=home)
    with pytest.raises(AuthError, match=r"expired at .*codex login"):
        await p.complete(messages=_msgs(), schema=_Sentiment, max_output_tokens=64)


# === Successful round-trip ===


@respx.mock
async def test_round_trip_includes_account_id(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    _write_oauth_creds(home, token="oat-live", account_id="acct-live")
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_response({"label": "bullish", "confidence": 0.7}))

    respx.post(_RESPONSES_URL).mock(side_effect=handler)

    p = OpenAIViaCodexCLI(codex_home=home, model="gpt-5.5-codex")
    result = await p.complete(messages=_msgs(), schema=_Sentiment, max_output_tokens=128)

    assert result.output.label == "bullish"
    assert result.output.confidence == pytest.approx(0.7)
    assert result.provider_id == "codex-cli/gpt-5.5-codex"
    assert result.usage.token_in == 31
    assert result.usage.token_out == 14

    assert captured["headers"]["authorization"] == "Bearer oat-live"
    assert captured["headers"]["chatgpt-account-id"] == "acct-live"

    body = captured["body"]
    assert body["model"] == "gpt-5.5-codex"
    assert body["max_output_tokens"] == 128
    assert body["input"] == [{"role": "user", "content": "Classify SPY momentum."}]
    assert len(body["tools"]) == 1
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["name"] == _TOOL_NAME
    assert body["tool_choice"] == {"type": "function", "name": _TOOL_NAME}


@respx.mock
async def test_round_trip_without_account_id(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    home.mkdir(parents=True)
    (home / "auth.json").write_text(
        json.dumps(
            {
                "tokens": {"access_token": "oat-no-acct", "refresh_token": "rt"},
                "last_refresh": _FUTURE_EXPIRY_MS,
            },
        ),
        encoding="utf-8",
    )
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json=_ok_response({"label": "neutral", "confidence": 0.5}))

    respx.post(_RESPONSES_URL).mock(side_effect=handler)

    p = OpenAIViaCodexCLI(codex_home=home)
    await p.complete(messages=_msgs(), schema=_Sentiment, max_output_tokens=32)

    assert captured["headers"]["authorization"] == "Bearer oat-no-acct"
    assert "chatgpt-account-id" not in captured["headers"]


# === HTTP error mapping ===


@respx.mock
async def test_complete_maps_401_to_auth_error(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    _write_oauth_creds(home)
    respx.post(_RESPONSES_URL).mock(return_value=httpx.Response(401))
    p = OpenAIViaCodexCLI(codex_home=home)
    with pytest.raises(AuthError):
        await p.complete(messages=_msgs(), schema=_Sentiment, max_output_tokens=32)


@respx.mock
async def test_complete_maps_429_to_rate_limit(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    _write_oauth_creds(home)
    respx.post(_RESPONSES_URL).mock(return_value=httpx.Response(429))
    p = OpenAIViaCodexCLI(codex_home=home)
    with pytest.raises(RateLimitError):
        await p.complete(messages=_msgs(), schema=_Sentiment, max_output_tokens=32)


@respx.mock
async def test_complete_maps_5xx_to_network_error(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    _write_oauth_creds(home)
    respx.post(_RESPONSES_URL).mock(return_value=httpx.Response(503))
    p = OpenAIViaCodexCLI(codex_home=home)
    with pytest.raises(NetworkError):
        await p.complete(messages=_msgs(), schema=_Sentiment, max_output_tokens=32)


@respx.mock
async def test_complete_maps_400_to_provider_error(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    _write_oauth_creds(home)
    respx.post(_RESPONSES_URL).mock(return_value=httpx.Response(400, json={"error": "bad"}))
    p = OpenAIViaCodexCLI(codex_home=home)
    with pytest.raises(ProviderError, match=r"HTTP 400"):
        await p.complete(messages=_msgs(), schema=_Sentiment, max_output_tokens=32)


@respx.mock
async def test_complete_maps_timeout(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    _write_oauth_creds(home)
    respx.post(_RESPONSES_URL).mock(side_effect=httpx.TimeoutException("slow"))
    p = OpenAIViaCodexCLI(codex_home=home)
    with pytest.raises(NetworkError, match=r"timeout"):
        await p.complete(messages=_msgs(), schema=_Sentiment, max_output_tokens=32)


# === Schema-violation paths ===


@respx.mock
async def test_complete_raises_when_no_function_call(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    _write_oauth_creds(home)
    respx.post(_RESPONSES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "resp_test",
                "object": "response",
                "model": "gpt-5.5-codex",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "I refuse"}],
                    },
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ),
    )
    p = OpenAIViaCodexCLI(codex_home=home)
    with pytest.raises(SchemaViolationError, match=r"function_call"):
        await p.complete(messages=_msgs(), schema=_Sentiment, max_output_tokens=32)


@respx.mock
async def test_complete_raises_when_unexpected_tool_name(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    _write_oauth_creds(home)
    respx.post(_RESPONSES_URL).mock(
        return_value=httpx.Response(
            200,
            json=_ok_response(
                {"label": "ok", "confidence": 0.5},
                tool_name="something_else",
            ),
        ),
    )
    p = OpenAIViaCodexCLI(codex_home=home)
    with pytest.raises(SchemaViolationError, match=r"unexpected function_call name"):
        await p.complete(messages=_msgs(), schema=_Sentiment, max_output_tokens=32)


@respx.mock
async def test_complete_raises_when_arguments_invalid_json(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    _write_oauth_creds(home)
    respx.post(_RESPONSES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "resp_test",
                "object": "response",
                "model": "gpt-5.5-codex",
                "output": [
                    {
                        "type": "function_call",
                        "name": _TOOL_NAME,
                        "arguments": "not-json",
                    },
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ),
    )
    p = OpenAIViaCodexCLI(codex_home=home)
    with pytest.raises(Exception, match=r"not valid JSON"):
        await p.complete(messages=_msgs(), schema=_Sentiment, max_output_tokens=32)


@respx.mock
async def test_complete_raises_when_arguments_fail_validation(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    _write_oauth_creds(home)
    respx.post(_RESPONSES_URL).mock(
        return_value=httpx.Response(
            200,
            json=_ok_response({"label": "x", "confidence": 1.5}),  # >1.0 invalid
        ),
    )
    p = OpenAIViaCodexCLI(codex_home=home)
    with pytest.raises(SchemaViolationError, match=r"schema validation"):
        await p.complete(messages=_msgs(), schema=_Sentiment, max_output_tokens=32)
