# SPDX-License-Identifier: MIT
# Ported from openclaw/openclaw at commit 22c9e82e835f4ef2cb3807f7fe6e148f4535a5ec:
#   - extensions/anthropic/cli-auth-seam.ts
#   - extensions/anthropic/stream-wrappers.ts (OAuth beta header set)
#   - src/agents/anthropic-transport-stream.ts (OAuth-vs-API-key marker)
#   - src/agents/provider-headers.live.test.ts (Messages endpoint shape)
# Original work (c) OpenClaw contributors, used under the MIT licence.
# CAQRS as a whole is Apache-2.0; this file retains its MIT origin.
"""Anthropic via Claude Code CLI credentials reuse — direct API path.

Reads the OAuth token from the local Claude CLI's stored session
(``~/.claude/.credentials.json`` or macOS Keychain) and posts directly
to ``https://api.anthropic.com/v1/messages`` under the user's
subscription. This is the path Anthropic publicly approves for
third-party agents per OpenClaw's contemporaneous note: "Anthropic staff
told us this OpenClaw path is allowed again."

Auth header selection follows the OpenClaw rule (``isAnthropicOAuthApiKey``):

- OAuth tokens from CLI (``sk-ant-oat`` substring): ``Authorization:
  Bearer ...`` plus the Claude-Code beta header set.
- Plain API keys (``sk-ant-...`` without ``oat``): ``x-api-key``.

Structured output uses Anthropic's tool-use format: a single tool
definition derived from the requested pydantic schema, ``tool_choice``
set to ``{type: tool, name: ...}`` to force the model to emit it, and
the response's ``tool_use`` content block validated against the schema.
"""

import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import httpx
from pydantic import BaseModel, ValidationError

from caqrs.providers._cli_creds import load_claude_cli_cred
from caqrs.providers.errors import (
    AuthError,
    NetworkError,
    ParseError,
    ProviderError,
    RateLimitError,
    SchemaViolationError,
)
from caqrs.providers.types import CompletionResult, Message, ProviderUsage

_MESSAGES_URL: Final[str] = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION: Final[str] = "2023-06-01"
_OAUTH_TOKEN_MARKER: Final[str] = "sk-ant-oat"

# Beta flags OpenClaw enables when the API key is an OAuth token. Required
# for subscription-backed requests; Anthropic rejects some of these on the
# API-key path so they are NOT sent when using x-api-key.
_OAUTH_BETAS: Final[tuple[str, ...]] = (
    "claude-code-20250219",
    "oauth-2025-04-20",
    "fine-grained-tool-streaming-2025-05-14",
    "interleaved-thinking-2025-05-14",
)

_HTTP_UNAUTHORIZED: Final[int] = 401
_HTTP_FORBIDDEN: Final[int] = 403
_HTTP_TOO_MANY_REQUESTS: Final[int] = 429
_HTTP_SERVER_ERROR: Final[int] = 500
_HTTP_CLIENT_ERROR: Final[int] = 400
_BAD_REQUEST_ERROR_BODY_LIMIT: Final[int] = 500


class AnthropicViaClaudeCLI:
    """Subscription-backed Anthropic provider reusing local Claude CLI credentials."""

    provider_id: str

    def __init__(
        self,
        *,
        model: str = "claude-opus-4-7",
        home_dir: Path | None = None,
        timeout_s: float = 120.0,
    ) -> None:
        self.provider_id = f"anthropic-cli/{model}"
        self._model = model
        self._home_dir = home_dir
        self._timeout_s = timeout_s

    def is_configured(self) -> bool:
        return load_claude_cli_cred(self._home_dir) is not None

    async def complete[T: BaseModel](
        self,
        *,
        messages: tuple[Message, ...],
        schema: type[T],
        max_output_tokens: int,
        temperature: float = 0.0,
    ) -> CompletionResult[T]:
        cred = load_claude_cli_cred(self._home_dir)
        if cred is None:
            raise AuthError(
                "Claude CLI not logged in; ~/.claude/.credentials.json missing.",
            )

        tool = _schema_to_tool_def(schema)
        payload = _build_request_payload(
            model=self._model,
            messages=messages,
            tool=tool,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
        )
        headers = _build_headers(cred.access_token)

        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                resp = await client.post(_MESSAGES_URL, headers=headers, json=payload)
        except httpx.TimeoutException as e:
            raise NetworkError(f"timeout after {self._timeout_s}s: {e}") from e
        except httpx.HTTPError as e:
            raise NetworkError(f"network failure: {e}") from e

        latency_ms = int((time.monotonic() - start) * 1000)
        _raise_for_status(resp)
        body = _decode_json(resp)
        return _parse_messages_response(body, schema, self.provider_id, latency_ms, tool["name"])


def _is_oauth_token(token: str) -> bool:
    """OpenClaw's marker rule (``isAnthropicOAuthApiKey``)."""
    return _OAUTH_TOKEN_MARKER in token


def _build_headers(access_token: str) -> dict[str, str]:
    headers: dict[str, str] = {
        "content-type": "application/json",
        "anthropic-version": _ANTHROPIC_VERSION,
    }
    if _is_oauth_token(access_token):
        headers["Authorization"] = f"Bearer {access_token}"
        headers["anthropic-beta"] = ",".join(_OAUTH_BETAS)
    else:
        headers["x-api-key"] = access_token
    return headers


def _schema_to_tool_def(schema: type[BaseModel]) -> dict[str, Any]:
    """Pydantic schema → Anthropic tool definition.

    Anthropic uses ``input_schema`` (vs OpenAI's ``parameters``) and a
    flat ``{name, description, input_schema}`` shape (no ``type:
    function`` wrapper).
    """
    json_schema = schema.model_json_schema()
    json_schema.pop("$schema", None)
    description = (schema.__doc__ or f"Emit a {schema.__name__} instance.").strip()
    return {
        "name": f"emit_{schema.__name__}",
        "description": description,
        "input_schema": json_schema,
    }


def _build_request_payload(
    *,
    model: str,
    messages: tuple[Message, ...],
    tool: dict[str, Any],
    max_output_tokens: int,
    temperature: float,
) -> dict[str, Any]:
    """Anthropic separates ``system`` from the messages array; concatenate any
    system messages into a single string and put the rest into ``messages``."""
    system_parts: list[str] = []
    body_messages: list[dict[str, str]] = []
    for m in messages:
        if m.role.value == "system":
            system_parts.append(m.content)
        else:
            body_messages.append({"role": m.role.value, "content": m.content})

    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_output_tokens,
        "temperature": temperature,
        "messages": body_messages,
        "tools": [tool],
        "tool_choice": {"type": "tool", "name": tool["name"]},
    }
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)
    return payload


def _raise_for_status(resp: httpx.Response) -> None:
    status = resp.status_code
    if status in (_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN):
        raise AuthError(f"auth failed: HTTP {status}")
    if status == _HTTP_TOO_MANY_REQUESTS:
        raise RateLimitError(f"rate limit: HTTP {status}")
    if status >= _HTTP_SERVER_ERROR:
        raise NetworkError(f"server error: HTTP {status}")
    if status >= _HTTP_CLIENT_ERROR:
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


def _parse_messages_response[T: BaseModel](
    body: dict[str, Any],
    schema: type[T],
    provider_id: str,
    latency_ms: int,
    expected_tool_name: str,
) -> CompletionResult[T]:
    content = body.get("content")
    if not isinstance(content, list) or not content:
        raise SchemaViolationError("response missing 'content' array")

    tool_use = next(
        (b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"),
        None,
    )
    if tool_use is None:
        raise SchemaViolationError(
            "model did not return a tool_use block; cannot extract structured output",
        )
    if tool_use.get("name") != expected_tool_name:
        raise SchemaViolationError(
            f"unexpected tool_use name: got {tool_use.get('name')!r}, "
            f"expected {expected_tool_name!r}",
        )
    tool_input = tool_use.get("input")
    if not isinstance(tool_input, dict):
        raise SchemaViolationError("tool_use.input is not a JSON object")

    try:
        output = schema.model_validate(tool_input)
    except ValidationError as e:
        raise SchemaViolationError(f"tool_use.input failed schema validation: {e}") from e

    usage = _build_usage(body.get("usage", {}), latency_ms)
    return CompletionResult[T](output=output, usage=usage, provider_id=provider_id)


def _build_usage(usage_raw: object, latency_ms: int) -> ProviderUsage:
    if not isinstance(usage_raw, dict):
        return ProviderUsage(token_in=0, token_out=0, latency_ms=latency_ms, cost_usd=Decimal(0))
    token_in = usage_raw.get("input_tokens", 0)
    token_out = usage_raw.get("output_tokens", 0)
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
