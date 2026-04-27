"""Tests for OpenAICompatProvider against a mocked OpenAI-compat endpoint."""

import json
from typing import Any

import httpx
import pytest
import respx
from pydantic import BaseModel, Field

from caqrs.providers import (
    AuthError,
    Message,
    NetworkError,
    OpenAICompatProvider,
    ProviderError,
    RateLimitError,
    Role,
)
from caqrs.providers.errors import ParseError, SchemaViolationError
from caqrs.providers.openai_compat import _schema_to_tool_def


class _Sentiment(BaseModel):
    """A market sentiment classification."""

    label: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


_BASE_URL = "http://localhost:4000/v1"
_COMPLETIONS_URL = f"{_BASE_URL}/chat/completions"
_OK_USAGE = {"prompt_tokens": 12, "completion_tokens": 7}


def _ok_response(arguments: dict[str, Any], tool_name: str = "emit__Sentiment") -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "model": "test-model",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(arguments),
                            },
                        },
                    ],
                },
                "finish_reason": "tool_calls",
            },
        ],
        "usage": _OK_USAGE,
    }


def _provider() -> OpenAICompatProvider:
    return OpenAICompatProvider(
        base_url=_BASE_URL,
        api_key="test-key",
        model="test-model",
    )


def _msgs() -> tuple[Message, ...]:
    return (Message(role=Role.USER, content="Classify SPY momentum."),)


# === Schema → tool definition ===


def test_schema_to_tool_def_drops_dollar_schema() -> None:
    tool = _schema_to_tool_def(_Sentiment)
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "emit__Sentiment"
    assert "$schema" not in tool["function"]["parameters"]
    assert "label" in tool["function"]["parameters"]["properties"]


def test_schema_to_tool_def_uses_docstring() -> None:
    tool = _schema_to_tool_def(_Sentiment)
    assert "market sentiment" in tool["function"]["description"]


# === Successful round-trip ===


@respx.mock
async def test_complete_returns_typed_output() -> None:
    respx.post(_COMPLETIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json=_ok_response({"label": "bullish", "confidence": 0.78}),
        ),
    )
    result = await _provider().complete(
        messages=_msgs(),
        schema=_Sentiment,
        max_output_tokens=128,
    )
    assert result.output.label == "bullish"
    assert result.output.confidence == pytest.approx(0.78)
    assert result.provider_id == "openai-compat/test-model"


@respx.mock
async def test_complete_extracts_token_usage() -> None:
    respx.post(_COMPLETIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json=_ok_response({"label": "neutral", "confidence": 0.5}),
        ),
    )
    result = await _provider().complete(
        messages=_msgs(),
        schema=_Sentiment,
        max_output_tokens=64,
    )
    assert result.usage.token_in == 12
    assert result.usage.token_out == 7
    assert result.usage.latency_ms >= 0


@respx.mock
async def test_complete_sends_correct_payload() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json=_ok_response({"label": "bear", "confidence": 0.1}),
        )

    respx.post(_COMPLETIONS_URL).mock(side_effect=handler)
    await _provider().complete(
        messages=_msgs(),
        schema=_Sentiment,
        max_output_tokens=42,
        temperature=0.3,
    )

    assert captured["headers"]["authorization"] == "Bearer test-key"
    body = captured["body"]
    assert body["model"] == "test-model"
    assert body["max_tokens"] == 42
    assert body["temperature"] == 0.3
    assert body["messages"] == [{"role": "user", "content": "Classify SPY momentum."}]
    assert len(body["tools"]) == 1
    assert body["tool_choice"]["function"]["name"] == "emit__Sentiment"


# === HTTP error mapping ===


@respx.mock
async def test_complete_maps_401_to_auth_error() -> None:
    respx.post(_COMPLETIONS_URL).mock(
        return_value=httpx.Response(401, json={"error": "bad key"}),
    )
    with pytest.raises(AuthError, match=r"HTTP 401"):
        await _provider().complete(
            messages=_msgs(),
            schema=_Sentiment,
            max_output_tokens=32,
        )


@respx.mock
async def test_complete_maps_403_to_auth_error() -> None:
    respx.post(_COMPLETIONS_URL).mock(return_value=httpx.Response(403))
    with pytest.raises(AuthError):
        await _provider().complete(
            messages=_msgs(),
            schema=_Sentiment,
            max_output_tokens=32,
        )


@respx.mock
async def test_complete_maps_429_to_rate_limit() -> None:
    respx.post(_COMPLETIONS_URL).mock(return_value=httpx.Response(429))
    with pytest.raises(RateLimitError):
        await _provider().complete(
            messages=_msgs(),
            schema=_Sentiment,
            max_output_tokens=32,
        )


@respx.mock
async def test_complete_maps_5xx_to_network_error() -> None:
    respx.post(_COMPLETIONS_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(NetworkError, match=r"HTTP 503"):
        await _provider().complete(
            messages=_msgs(),
            schema=_Sentiment,
            max_output_tokens=32,
        )


@respx.mock
async def test_complete_maps_400_to_provider_error() -> None:
    respx.post(_COMPLETIONS_URL).mock(
        return_value=httpx.Response(400, json={"error": "bad payload"}),
    )
    with pytest.raises(ProviderError, match=r"HTTP 400"):
        await _provider().complete(
            messages=_msgs(),
            schema=_Sentiment,
            max_output_tokens=32,
        )


@respx.mock
async def test_complete_maps_timeout_to_network_error() -> None:
    respx.post(_COMPLETIONS_URL).mock(side_effect=httpx.TimeoutException("slow"))
    with pytest.raises(NetworkError, match=r"timeout"):
        await _provider().complete(
            messages=_msgs(),
            schema=_Sentiment,
            max_output_tokens=32,
        )


@respx.mock
async def test_complete_maps_connect_error_to_network_error() -> None:
    respx.post(_COMPLETIONS_URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(NetworkError, match=r"network"):
        await _provider().complete(
            messages=_msgs(),
            schema=_Sentiment,
            max_output_tokens=32,
        )


# === Schema-violation paths ===


@respx.mock
async def test_complete_raises_when_no_tool_call() -> None:
    respx.post(_COMPLETIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "I refuse"},
                        "finish_reason": "stop",
                    },
                ],
                "usage": _OK_USAGE,
            },
        ),
    )
    with pytest.raises(SchemaViolationError, match=r"tool_call"):
        await _provider().complete(
            messages=_msgs(),
            schema=_Sentiment,
            max_output_tokens=32,
        )


@respx.mock
async def test_complete_raises_when_arguments_invalid_json() -> None:
    respx.post(_COMPLETIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": "emit__Sentiment",
                                        "arguments": "not-json",
                                    },
                                },
                            ],
                        },
                    },
                ],
                "usage": _OK_USAGE,
            },
        ),
    )
    with pytest.raises(ParseError, match=r"not valid JSON"):
        await _provider().complete(
            messages=_msgs(),
            schema=_Sentiment,
            max_output_tokens=32,
        )


@respx.mock
async def test_complete_raises_when_arguments_fail_schema_validation() -> None:
    respx.post(_COMPLETIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json=_ok_response({"label": "bullish", "confidence": 1.5}),  # >1.0 invalid
        ),
    )
    with pytest.raises(SchemaViolationError, match=r"schema validation"):
        await _provider().complete(
            messages=_msgs(),
            schema=_Sentiment,
            max_output_tokens=32,
        )


@respx.mock
async def test_complete_raises_on_non_object_body() -> None:
    respx.post(_COMPLETIONS_URL).mock(
        return_value=httpx.Response(200, json=["not", "an", "object"]),
    )
    with pytest.raises(ParseError, match=r"not a JSON object"):
        await _provider().complete(
            messages=_msgs(),
            schema=_Sentiment,
            max_output_tokens=32,
        )
