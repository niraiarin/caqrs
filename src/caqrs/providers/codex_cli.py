# SPDX-License-Identifier: MIT
# Ported from openclaw/openclaw at commit 22c9e82e835f4ef2cb3807f7fe6e148f4535a5ec:
#   - extensions/openai/openai-codex-provider.ts (route definition)
#   - extensions/openai/base-url.ts (chatgpt.com/backend-api/codex base URL)
#   - src/infra/provider-usage.fetch.codex.ts (auth/header pattern)
#   - extensions/codex/src/app-server/auth-bridge.ts (account_id usage)
# Original work (c) OpenClaw contributors, used under the MIT licence.
# CAQRS as a whole is Apache-2.0; this file retains its MIT origin.
"""OpenAI Codex via Codex CLI OAuth session reuse — direct API path.

Reads the OAuth token (and ``account_id``) from the local Codex CLI's
stored session (``~/.codex/auth.json`` or macOS Keychain) and posts to
``https://chatgpt.com/backend-api/codex/responses`` under the user's
ChatGPT subscription. This is the ``openai-codex`` route in OpenClaw's
provider taxonomy: subscription OAuth via PI runner, distinct from the
``codex/*`` app-server harness and the direct ``api.openai.com`` API-key
route. Per OpenClaw's documentation, "OpenAI explicitly supports
subscription OAuth usage in external tools and workflows like OpenClaw."

Wire format follows the OpenAI Responses API:

- ``input`` list of ``{role, content}`` instead of ``messages``.
- Tools are flat (no ``function`` wrapper): ``{type: function, name,
  description, parameters}``.
- ``tool_choice``: ``{type: function, name: ...}``.
- Response: ``output`` array with ``type: function_call`` entries whose
  ``arguments`` is a JSON string.

Headers:

- ``Authorization: Bearer <access_token>``
- ``ChatGPT-Account-Id: <account_id>`` when present on the credential.
- ``content-type: application/json``
"""

import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import httpx
from pydantic import BaseModel, ValidationError

from caqrs.providers._cli_creds import (
    format_expiry_iso,
    is_cred_fresh,
    load_codex_cli_cred,
)
from caqrs.providers.errors import (
    AuthError,
    NetworkError,
    ParseError,
    ProviderError,
    RateLimitError,
    SchemaViolationError,
)
from caqrs.providers.types import CompletionResult, Message, ProviderUsage

_RESPONSES_URL: Final[str] = "https://chatgpt.com/backend-api/codex/responses"

_HTTP_UNAUTHORIZED: Final[int] = 401
_HTTP_FORBIDDEN: Final[int] = 403
_HTTP_TOO_MANY_REQUESTS: Final[int] = 429
_HTTP_SERVER_ERROR: Final[int] = 500
_HTTP_CLIENT_ERROR: Final[int] = 400
_BAD_REQUEST_ERROR_BODY_LIMIT: Final[int] = 500


class OpenAIViaCodexCLI:
    """Subscription-backed OpenAI Codex provider reusing local Codex CLI OAuth."""

    provider_id: str

    def __init__(
        self,
        *,
        model: str = "gpt-5.5-codex",
        codex_home: Path | None = None,
        timeout_s: float = 120.0,
    ) -> None:
        self.provider_id = f"codex-cli/{model}"
        self._model = model
        self._codex_home = codex_home
        self._timeout_s = timeout_s

    def is_configured(self) -> bool:
        return load_codex_cli_cred(self._codex_home) is not None

    async def complete[T: BaseModel](
        self,
        *,
        messages: tuple[Message, ...],
        schema: type[T],
        max_output_tokens: int,
        temperature: float = 0.0,
    ) -> CompletionResult[T]:
        cred = load_codex_cli_cred(self._codex_home)
        if cred is None:
            raise AuthError(
                "Codex CLI not logged in; ~/.codex/auth.json missing.",
            )
        if not is_cred_fresh(cred):
            raise AuthError(
                f"Codex CLI cred expired at {format_expiry_iso(cred)}. "
                "Run `codex login` to renew. Auto-refresh is out of scope per ADR-0003.",
            )

        tool = _schema_to_tool_def(schema)
        payload = _build_request_payload(
            model=self._model,
            messages=messages,
            tool=tool,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
        )
        headers = _build_headers(cred.access_token, cred.account_id)

        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                resp = await client.post(_RESPONSES_URL, headers=headers, json=payload)
        except httpx.TimeoutException as e:
            raise NetworkError(f"timeout after {self._timeout_s}s: {e}") from e
        except httpx.HTTPError as e:
            raise NetworkError(f"network failure: {e}") from e

        latency_ms = int((time.monotonic() - start) * 1000)
        _raise_for_status(resp)
        body = _decode_json(resp)
        return _parse_responses_body(body, schema, self.provider_id, latency_ms, tool["name"])


def _build_headers(access_token: str, account_id: str | None) -> dict[str, str]:
    headers: dict[str, str] = {
        "content-type": "application/json",
        "Authorization": f"Bearer {access_token}",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    return headers


def _schema_to_tool_def(schema: type[BaseModel]) -> dict[str, Any]:
    """Pydantic schema → OpenAI Responses API tool definition.

    The Responses API uses a flat tool shape: fields ``name``,
    ``description``, ``parameters`` live directly under the ``function``-
    typed entry, not nested under a ``function`` key as in Chat Completions.
    """
    json_schema = schema.model_json_schema()
    json_schema.pop("$schema", None)
    description = (schema.__doc__ or f"Emit a {schema.__name__} instance.").strip()
    return {
        "type": "function",
        "name": f"emit_{schema.__name__}",
        "description": description,
        "parameters": json_schema,
    }


def _build_request_payload(
    *,
    model: str,
    messages: tuple[Message, ...],
    tool: dict[str, Any],
    max_output_tokens: int,
    temperature: float,
) -> dict[str, Any]:
    """OpenAI Responses API request body.

    Uses the simple ``input`` list form ``[{role, content}, ...]`` rather
    than the structured ``[{type: message, content: [{type: input_text,
    text}]}, ...]`` form; OpenAI accepts both.
    """
    input_items = [{"role": m.role.value, "content": m.content} for m in messages]
    return {
        "model": model,
        "input": input_items,
        "max_output_tokens": max_output_tokens,
        "temperature": temperature,
        "tools": [tool],
        "tool_choice": {"type": "function", "name": tool["name"]},
    }


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


def _parse_responses_body[T: BaseModel](
    body: dict[str, Any],
    schema: type[T],
    provider_id: str,
    latency_ms: int,
    expected_tool_name: str,
) -> CompletionResult[T]:
    """Extract the first ``function_call`` from ``output`` and validate against schema."""
    output = body.get("output")
    if not isinstance(output, list) or not output:
        raise SchemaViolationError("response missing 'output' array")

    function_call = next(
        (item for item in output if isinstance(item, dict) and item.get("type") == "function_call"),
        None,
    )
    if function_call is None:
        raise SchemaViolationError(
            "model did not return a function_call; cannot extract structured output",
        )
    if function_call.get("name") != expected_tool_name:
        raise SchemaViolationError(
            f"unexpected function_call name: got {function_call.get('name')!r}, "
            f"expected {expected_tool_name!r}",
        )

    arguments_raw = function_call.get("arguments")
    if not isinstance(arguments_raw, str):
        raise SchemaViolationError("function_call.arguments is not a JSON string")

    try:
        arguments = json.loads(arguments_raw)
    except ValueError as e:
        raise ParseError(f"function_call arguments not valid JSON: {e}") from e

    try:
        parsed = schema.model_validate(arguments)
    except ValidationError as e:
        raise SchemaViolationError(f"function_call arguments failed schema validation: {e}") from e

    usage = _build_usage(body.get("usage", {}), latency_ms)
    return CompletionResult[T](output=parsed, usage=usage, provider_id=provider_id)


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
