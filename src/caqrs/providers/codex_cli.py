"""OpenAI via Codex CLI OAuth session reuse.

Status: P1.0 stub. Real implementation lands in P1.1, ported from
OpenClaw ``extensions/codex/src/app-server/auth-bridge.ts`` (MIT-licensed;
commit hash and SPDX header will be added at port time).

Reuses the OAuth session of a logged-in ``codex`` CLI on the host so
calls run under the user's ChatGPT subscription. Same compliance posture
as ``AnthropicViaClaudeCLI``: subscription credentials are accessed via
the official CLI's stored session, not via OAuth-token-extraction hacks.
"""

from pydantic import BaseModel

from caqrs.providers.types import CompletionResult, Message


class OpenAIViaCodexCLI:
    """Subscription-backed OpenAI provider reusing local Codex CLI OAuth session."""

    provider_id: str

    def __init__(self, *, model: str = "gpt-5.5-codex") -> None:
        self.provider_id = f"codex-cli/{model}"
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
            "OpenAIViaCodexCLI: OAuth session reuse from local Codex CLI "
            "lands in P1.1 (port from OpenClaw extensions/codex/). "
            "Use OpenAICompatProvider with a LiteLLM gateway in the meantime.",
        )
