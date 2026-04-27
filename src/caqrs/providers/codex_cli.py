"""OpenAI via Codex CLI OAuth session reuse.

Status: P1.1.b. Cred reading (file + macOS Keychain) is wired up via
``is_configured()``; HTTP transport and structured output land in P1.1.c
and P1.1.d respectively.

Reuses the OAuth session of a logged-in ``codex`` CLI on the host so
calls run under the user's ChatGPT subscription. Same compliance posture
as ``AnthropicViaClaudeCLI``: subscription credentials are accessed via
the official CLI's stored session, never via OAuth-token-extraction hacks.
"""

from pathlib import Path

from pydantic import BaseModel

from caqrs.providers._cli_creds import load_codex_cli_cred
from caqrs.providers.types import CompletionResult, Message


class OpenAIViaCodexCLI:
    """Subscription-backed OpenAI provider reusing local Codex CLI OAuth session."""

    provider_id: str

    def __init__(
        self,
        *,
        model: str = "gpt-5.5-codex",
        codex_home: Path | None = None,
    ) -> None:
        self.provider_id = f"codex-cli/{model}"
        self._model = model
        self._codex_home = codex_home

    def is_configured(self) -> bool:
        """True iff a valid Codex CLI cred is reachable (file or Keychain)."""
        return load_codex_cli_cred(self._codex_home) is not None

    async def complete[T: BaseModel](
        self,
        *,
        messages: tuple[Message, ...],
        schema: type[T],
        max_output_tokens: int,
        temperature: float = 0.0,
    ) -> CompletionResult[T]:
        del messages, schema, max_output_tokens, temperature  # P1.1.b stub
        raise NotImplementedError(
            "OpenAIViaCodexCLI: HTTP transport lands in P1.1.c. "
            "Use OpenAICompatProvider with a LiteLLM gateway in the meantime.",
        )
