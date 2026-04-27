"""Anthropic via Claude Code CLI credentials reuse.

Status: P1.0 stub. Real implementation lands in P1.1, ported from
OpenClaw ``extensions/anthropic/cli-auth-seam.ts`` and ``cli-backend.ts``
(MIT-licensed; commit hash and SPDX header will be added at port time).

Per OpenClaw's provider docs, ``"OpenClaw-style Claude CLI usage is allowed
again"`` per Anthropic staff communication. CAQRS adopts the same path:
detect a logged-in ``claude`` CLI on the host, read its credentials, and
issue Anthropic Messages API calls under the user's subscription.
"""

from pydantic import BaseModel

from caqrs.providers.types import CompletionResult, Message


class AnthropicViaClaudeCLI:
    """Subscription-backed Anthropic provider reusing local Claude CLI credentials."""

    provider_id: str

    def __init__(self, *, model: str = "claude-opus-4-7") -> None:
        self.provider_id = f"anthropic-cli/{model}"
        self._model = model

    async def complete[T: BaseModel](
        self,
        *,
        messages: tuple[Message, ...],
        schema: type[T],
        max_output_tokens: int,
        temperature: float = 0.0,
    ) -> CompletionResult[T]:
        del messages, schema, max_output_tokens, temperature  # P1.0 stub
        raise NotImplementedError(
            "AnthropicViaClaudeCLI: credentials reuse from local Claude CLI "
            "lands in P1.1 (port from OpenClaw extensions/anthropic/). "
            "Use OpenAICompatProvider with a LiteLLM gateway in the meantime.",
        )
