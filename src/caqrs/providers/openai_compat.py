"""Generic OpenAI-compatible provider (LiteLLM gateway, vLLM, LM Studio, Ollama).

Uses ``POST {base_url}/chat/completions`` with a ``tools`` definition derived
from the requested pydantic schema and ``tool_choice`` forcing the model to
emit exactly that tool. The tool-call ``arguments`` JSON is then validated
against the pydantic schema and returned as a typed ``CompletionResult``.

This provider does **not** handle subscription credentials. It expects an
explicit ``base_url`` (with API version prefix, e.g. ``http://host:port/v1``)
and an ``api_key`` issued by the gateway itself.
"""

import json
import time
from decimal import Decimal
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from caqrs.providers.errors import (
    AuthError,
    NetworkError,
    ParseError,
    ProviderError,
    RateLimitError,
    SchemaViolationError,
)
from caqrs.providers.types import CompletionResult, Message, ProviderUsage

_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403
_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_SERVER_ERROR = 500
_BAD_REQUEST_ERROR_BODY_LIMIT = 500


class OpenAICompatProvider:
    """Generic OpenAI-compatible HTTP provider."""

    provider_id: str

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: float = 60.0,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        if not api_key:
            raise ValueError("api_key is required")
        if not model:
            raise ValueError("model is required")
        self.provider_id = f"openai-compat/{model}"
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout_s = timeout_s

    async def complete[T: BaseModel](
        self,
        *,
        messages: tuple[Message, ...],
        schema: type[T],
        max_output_tokens: int,
        temperature: float = 0.0,
    ) -> CompletionResult[T]:
        tool = _schema_to_tool_def(schema)
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": m.role.value, "content": m.content} for m in messages],
            "max_tokens": max_output_tokens,
            "temperature": temperature,
            "tools": [tool],
            "tool_choice": {
                "type": "function",
                "function": {"name": tool["function"]["name"]},
            },
        }
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                resp = await client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException as e:
            raise NetworkError(f"timeout after {self._timeout_s}s: {e}") from e
        except httpx.HTTPError as e:
            raise NetworkError(f"network failure: {e}") from e

        latency_ms = int((time.monotonic() - start) * 1000)
        _raise_for_status(resp)
        body = _decode_json(resp)
        return _parse_tool_call_response(body, schema, self.provider_id, latency_ms)


def _schema_to_tool_def(schema: type[BaseModel]) -> dict[str, Any]:
    """Convert a pydantic schema to an OpenAI tool definition.

    The ``$schema`` URI is dropped because some OpenAI-compatible backends
    reject the JSON Schema 2020-12 marker.
    """
    json_schema = schema.model_json_schema()
    json_schema.pop("$schema", None)
    description = (schema.__doc__ or f"Emit a {schema.__name__} instance.").strip()
    return {
        "type": "function",
        "function": {
            "name": f"emit_{schema.__name__}",
            "description": description,
            "parameters": json_schema,
        },
    }


def _raise_for_status(resp: httpx.Response) -> None:
    """Map HTTP status to ``ProviderError`` subclasses."""
    status = resp.status_code
    if status in (_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN):
        raise AuthError(f"auth failed: HTTP {status}")
    if status == _HTTP_TOO_MANY_REQUESTS:
        raise RateLimitError(f"rate limit: HTTP {status}")
    if status >= _HTTP_SERVER_ERROR:
        raise NetworkError(f"server error: HTTP {status}")
    if status >= _HTTP_UNAUTHORIZED // 100 * 100:  # any 4xx not handled above
        body_preview = resp.text[:_BAD_REQUEST_ERROR_BODY_LIMIT]
        raise ProviderError(f"HTTP {status}: {body_preview}")


def _decode_json(resp: httpx.Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError as e:
        raise ParseError(f"invalid JSON response: {e}") from e
    if not isinstance(body, dict):
        raise ParseError("response body is not a JSON object")
    return body


def _parse_tool_call_response[T: BaseModel](
    body: dict[str, Any],
    schema: type[T],
    provider_id: str,
    latency_ms: int,
) -> CompletionResult[T]:
    """Extract the first tool_call.arguments and validate it against the schema."""
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise SchemaViolationError("response missing 'choices' array")
    first = choices[0]
    if not isinstance(first, dict):
        raise SchemaViolationError("choices[0] is not an object")

    message = first.get("message")
    if not isinstance(message, dict):
        raise SchemaViolationError("choices[0].message missing")

    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list) or not tool_calls:
        raise SchemaViolationError(
            "model did not return a tool_call; cannot extract structured output",
        )

    first_call = tool_calls[0]
    if not isinstance(first_call, dict):
        raise SchemaViolationError("tool_calls[0] is not an object")
    function = first_call.get("function")
    if not isinstance(function, dict):
        raise SchemaViolationError("tool_calls[0].function missing")

    arguments_raw = function.get("arguments")
    if not isinstance(arguments_raw, str):
        raise SchemaViolationError("tool_calls[0].function.arguments is not a string")

    try:
        arguments = json.loads(arguments_raw)
    except ValueError as e:
        raise ParseError(f"tool_call arguments not valid JSON: {e}") from e

    try:
        output = schema.model_validate(arguments)
    except ValidationError as e:
        raise SchemaViolationError(f"tool_call arguments failed schema validation: {e}") from e

    usage_raw = body.get("usage", {})
    usage = _build_usage(usage_raw, latency_ms)

    return CompletionResult[T](
        output=output,
        usage=usage,
        provider_id=provider_id,
    )


def _build_usage(usage_raw: object, latency_ms: int) -> ProviderUsage:
    """Extract token counts from the usage block. Cost is left at 0 (P1.1.5
    does not yet apply per-model rate tables; the orchestrator can compute
    cost from token counts later)."""
    if not isinstance(usage_raw, dict):
        return ProviderUsage(token_in=0, token_out=0, latency_ms=latency_ms, cost_usd=Decimal(0))
    token_in = usage_raw.get("prompt_tokens", 0)
    token_out = usage_raw.get("completion_tokens", 0)
    return ProviderUsage(
        token_in=int(token_in)
        if isinstance(token_in, int) and not isinstance(token_in, bool)
        else 0,
        token_out=(
            int(token_out) if isinstance(token_out, int) and not isinstance(token_out, bool) else 0
        ),
        latency_ms=latency_ms,
        cost_usd=Decimal(0),
    )
