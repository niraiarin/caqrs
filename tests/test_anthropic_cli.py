"""Tests for AnthropicViaClaudeCLI HTTP transport."""

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import BaseModel, Field

from caqrs.providers import (
    AnthropicViaClaudeCLI,
    AuthError,
    Message,
    NetworkError,
    ProviderError,
    RateLimitError,
    Role,
)
from caqrs.providers.anthropic_cli import (
    _build_headers,
    _is_oauth_token,
    _schema_to_tool_def,
)
from caqrs.providers.errors import SchemaViolationError


class _Pulse(BaseModel):
    """Heart-rate reading from a wearable device."""

    bpm: int = Field(ge=0, le=300)
    note: str = Field(min_length=1)


_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
_FUTURE_EXPIRY_MS = int((time.time() + 3600) * 1000)
_TOOL_NAME = "emit__Pulse"


def _write_oauth_creds(home: Path, token: str = "sk-ant-oat-test") -> None:
    creds_dir = home / ".claude"
    creds_dir.mkdir(exist_ok=True)
    (creds_dir / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": token,
                    "refreshToken": "rt-test",
                    "expiresAt": _FUTURE_EXPIRY_MS,
                },
            },
        ),
        encoding="utf-8",
    )


def _write_api_key_creds(home: Path, key: str = "sk-ant-api01-test") -> None:
    """Write a Claude file in 'token' shape (no refresh) so the provider treats
    the access_token as a non-OAuth API key."""
    creds_dir = home / ".claude"
    creds_dir.mkdir(exist_ok=True)
    (creds_dir / ".credentials.json").write_text(
        json.dumps(
            {"claudeAiOauth": {"accessToken": key, "expiresAt": _FUTURE_EXPIRY_MS}},
        ),
        encoding="utf-8",
    )


def _ok_response(input_dict: dict[str, Any], tool_name: str = _TOOL_NAME) -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-4-7",
        "content": [
            {"type": "tool_use", "id": "toolu_test", "name": tool_name, "input": input_dict},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 42, "output_tokens": 17},
    }


def _msgs() -> tuple[Message, ...]:
    return (Message(role=Role.USER, content="What's the BPM?"),)


# === OAuth marker detection ===


def test_oauth_token_detected_by_marker() -> None:
    assert _is_oauth_token("sk-ant-oat-foo")
    assert _is_oauth_token("xxx-sk-ant-oat-yyy")
    assert not _is_oauth_token("sk-ant-api01-foo")
    assert not _is_oauth_token("sk-ant-test")


# === Header selection ===


def test_oauth_headers() -> None:
    headers = _build_headers("sk-ant-oat-test")
    assert headers["Authorization"] == "Bearer sk-ant-oat-test"
    assert "x-api-key" not in headers
    assert headers["anthropic-version"] == "2023-06-01"
    assert "claude-code-20250219" in headers["anthropic-beta"]
    assert "oauth-2025-04-20" in headers["anthropic-beta"]


def test_api_key_headers() -> None:
    headers = _build_headers("sk-ant-api01-test")
    assert headers["x-api-key"] == "sk-ant-api01-test"
    assert "Authorization" not in headers
    assert headers["anthropic-version"] == "2023-06-01"
    assert "anthropic-beta" not in headers


# === Tool definition ===


def test_schema_to_tool_def_uses_input_schema() -> None:
    tool = _schema_to_tool_def(_Pulse)
    assert tool["name"] == _TOOL_NAME
    assert "input_schema" in tool
    assert "parameters" not in tool
    assert "$schema" not in tool["input_schema"]
    assert "wearable" in tool["description"]


# === is_configured / pre-call gate ===


def test_is_configured_false_without_creds(tmp_path: Path) -> None:
    p = AnthropicViaClaudeCLI(home_dir=tmp_path)
    assert p.is_configured() is False


def test_is_configured_true_with_creds(tmp_path: Path) -> None:
    _write_oauth_creds(tmp_path)
    p = AnthropicViaClaudeCLI(home_dir=tmp_path)
    assert p.is_configured() is True


async def test_complete_raises_when_not_logged_in(tmp_path: Path) -> None:
    p = AnthropicViaClaudeCLI(home_dir=tmp_path)
    with pytest.raises(AuthError, match="not logged in"):
        await p.complete(messages=_msgs(), schema=_Pulse, max_output_tokens=64)


# === Successful round-trip (OAuth) ===


@respx.mock
async def test_oauth_round_trip(tmp_path: Path) -> None:
    _write_oauth_creds(tmp_path, token="sk-ant-oat-live")
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_response({"bpm": 72, "note": "resting"}))

    respx.post(_MESSAGES_URL).mock(side_effect=handler)

    p = AnthropicViaClaudeCLI(home_dir=tmp_path)
    result = await p.complete(messages=_msgs(), schema=_Pulse, max_output_tokens=128)

    assert result.output.bpm == 72
    assert result.output.note == "resting"
    assert result.provider_id == "anthropic-cli/claude-opus-4-7"
    assert result.usage.token_in == 42
    assert result.usage.token_out == 17

    assert captured["headers"]["authorization"] == "Bearer sk-ant-oat-live"
    assert "x-api-key" not in captured["headers"]
    assert "claude-code-20250219" in captured["headers"]["anthropic-beta"]

    body = captured["body"]
    assert body["model"] == "claude-opus-4-7"
    assert body["max_tokens"] == 128
    assert len(body["tools"]) == 1
    assert body["tools"][0]["name"] == _TOOL_NAME
    assert "input_schema" in body["tools"][0]
    assert body["tool_choice"] == {"type": "tool", "name": _TOOL_NAME}


# === API-key round-trip (auth header switch) ===


@respx.mock
async def test_api_key_round_trip(tmp_path: Path) -> None:
    _write_api_key_creds(tmp_path, key="sk-ant-api01-live")
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json=_ok_response({"bpm": 100, "note": "running"}))

    respx.post(_MESSAGES_URL).mock(side_effect=handler)

    p = AnthropicViaClaudeCLI(home_dir=tmp_path)
    await p.complete(messages=_msgs(), schema=_Pulse, max_output_tokens=64)

    assert captured["headers"]["x-api-key"] == "sk-ant-api01-live"
    assert "authorization" not in captured["headers"]
    assert "anthropic-beta" not in captured["headers"]


# === System message extraction ===


@respx.mock
async def test_system_messages_consolidated(tmp_path: Path) -> None:
    _write_oauth_creds(tmp_path)
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_response({"bpm": 60, "note": "z"}))

    respx.post(_MESSAGES_URL).mock(side_effect=handler)

    msgs = (
        Message(role=Role.SYSTEM, content="You are a heart-rate reporter."),
        Message(role=Role.SYSTEM, content="Be concise."),
        Message(role=Role.USER, content="Now."),
    )
    p = AnthropicViaClaudeCLI(home_dir=tmp_path)
    await p.complete(messages=msgs, schema=_Pulse, max_output_tokens=32)

    assert "You are a heart-rate reporter." in captured["body"]["system"]
    assert "Be concise." in captured["body"]["system"]
    assert all(m["role"] != "system" for m in captured["body"]["messages"])


# === HTTP error mapping ===


@respx.mock
async def test_complete_maps_401_to_auth_error(tmp_path: Path) -> None:
    _write_oauth_creds(tmp_path)
    respx.post(_MESSAGES_URL).mock(return_value=httpx.Response(401))
    p = AnthropicViaClaudeCLI(home_dir=tmp_path)
    with pytest.raises(AuthError, match=r"HTTP 401"):
        await p.complete(messages=_msgs(), schema=_Pulse, max_output_tokens=32)


@respx.mock
async def test_complete_maps_403_to_auth_error(tmp_path: Path) -> None:
    _write_oauth_creds(tmp_path)
    respx.post(_MESSAGES_URL).mock(return_value=httpx.Response(403))
    p = AnthropicViaClaudeCLI(home_dir=tmp_path)
    with pytest.raises(AuthError):
        await p.complete(messages=_msgs(), schema=_Pulse, max_output_tokens=32)


@respx.mock
async def test_complete_maps_429_to_rate_limit(tmp_path: Path) -> None:
    _write_oauth_creds(tmp_path)
    respx.post(_MESSAGES_URL).mock(return_value=httpx.Response(429))
    p = AnthropicViaClaudeCLI(home_dir=tmp_path)
    with pytest.raises(RateLimitError):
        await p.complete(messages=_msgs(), schema=_Pulse, max_output_tokens=32)


@respx.mock
async def test_complete_maps_5xx_to_network_error(tmp_path: Path) -> None:
    _write_oauth_creds(tmp_path)
    respx.post(_MESSAGES_URL).mock(return_value=httpx.Response(503))
    p = AnthropicViaClaudeCLI(home_dir=tmp_path)
    with pytest.raises(NetworkError, match=r"HTTP 503"):
        await p.complete(messages=_msgs(), schema=_Pulse, max_output_tokens=32)


@respx.mock
async def test_complete_maps_400_to_provider_error(tmp_path: Path) -> None:
    _write_oauth_creds(tmp_path)
    respx.post(_MESSAGES_URL).mock(return_value=httpx.Response(400, json={"error": "bad"}))
    p = AnthropicViaClaudeCLI(home_dir=tmp_path)
    with pytest.raises(ProviderError, match=r"HTTP 400"):
        await p.complete(messages=_msgs(), schema=_Pulse, max_output_tokens=32)


@respx.mock
async def test_complete_maps_timeout(tmp_path: Path) -> None:
    _write_oauth_creds(tmp_path)
    respx.post(_MESSAGES_URL).mock(side_effect=httpx.TimeoutException("slow"))
    p = AnthropicViaClaudeCLI(home_dir=tmp_path)
    with pytest.raises(NetworkError, match=r"timeout"):
        await p.complete(messages=_msgs(), schema=_Pulse, max_output_tokens=32)


@respx.mock
async def test_complete_maps_connect_error(tmp_path: Path) -> None:
    _write_oauth_creds(tmp_path)
    respx.post(_MESSAGES_URL).mock(side_effect=httpx.ConnectError("refused"))
    p = AnthropicViaClaudeCLI(home_dir=tmp_path)
    with pytest.raises(NetworkError, match=r"network"):
        await p.complete(messages=_msgs(), schema=_Pulse, max_output_tokens=32)


# === Schema-violation paths ===


@respx.mock
async def test_complete_raises_when_no_tool_use_block(tmp_path: Path) -> None:
    _write_oauth_creds(tmp_path)
    respx.post(_MESSAGES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude",
                "content": [{"type": "text", "text": "I refuse"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ),
    )
    p = AnthropicViaClaudeCLI(home_dir=tmp_path)
    with pytest.raises(SchemaViolationError, match=r"tool_use"):
        await p.complete(messages=_msgs(), schema=_Pulse, max_output_tokens=32)


@respx.mock
async def test_complete_raises_when_unexpected_tool_name(tmp_path: Path) -> None:
    _write_oauth_creds(tmp_path)
    respx.post(_MESSAGES_URL).mock(
        return_value=httpx.Response(
            200,
            json=_ok_response({"bpm": 60, "note": "ok"}, tool_name="something_else"),
        ),
    )
    p = AnthropicViaClaudeCLI(home_dir=tmp_path)
    with pytest.raises(SchemaViolationError, match=r"unexpected tool_use name"):
        await p.complete(messages=_msgs(), schema=_Pulse, max_output_tokens=32)


@respx.mock
async def test_complete_raises_when_input_fails_validation(tmp_path: Path) -> None:
    _write_oauth_creds(tmp_path)
    respx.post(_MESSAGES_URL).mock(
        return_value=httpx.Response(
            200,
            json=_ok_response({"bpm": 999, "note": "out_of_range"}),
        ),
    )
    p = AnthropicViaClaudeCLI(home_dir=tmp_path)
    with pytest.raises(SchemaViolationError, match=r"schema validation"):
        await p.complete(messages=_msgs(), schema=_Pulse, max_output_tokens=32)
