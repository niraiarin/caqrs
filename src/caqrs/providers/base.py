"""LLMProvider protocol: the abstract contract every concrete provider satisfies.

Three concrete providers are planned (P1.0 ships stubs, P1.1 ports the
subscription credential paths from OpenClaw):

- ``AnthropicViaClaudeCLI`` — reuses local Claude Code CLI credentials.
  Pattern ported from OpenClaw ``extensions/anthropic/cli-auth-seam.ts``
  and ``cli-backend.ts``.
- ``OpenAIViaCodexCLI`` — reuses local Codex CLI OAuth session.
  Pattern ported from OpenClaw
  ``extensions/codex/src/app-server/auth-bridge.ts``.
- ``OpenAICompatProvider`` — generic OpenAI-compatible endpoint
  (LiteLLM gateway, vLLM, LM Studio, Ollama, etc.).

The protocol is structural; implementers do not inherit. The registry
orders providers and falls through on ``ProviderError``.
"""

from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from caqrs.providers.types import CompletionResult, Message


@runtime_checkable
class LLMProvider(Protocol):
    """Typed LLM provider returning structured pydantic outputs.

    Implementations encapsulate transport (httpx / SDK) and credentials
    (env var, CLI reuse, OAuth) behind ``complete``. Errors map onto
    ``caqrs.providers.errors.ProviderError`` subclasses; non-provider
    exceptions propagate.
    """

    provider_id: str

    async def complete[T: BaseModel](
        self,
        *,
        messages: tuple[Message, ...],
        schema: type[T],
        max_output_tokens: int,
        temperature: float = 0.0,
    ) -> CompletionResult[T]: ...
