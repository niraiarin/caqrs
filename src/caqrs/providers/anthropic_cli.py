"""Anthropic via Claude Code CLI credentials reuse.

Status: P1.1.b. Cred reading (file + macOS Keychain) is wired up via
``is_configured()``; HTTP transport and structured output land in P1.1.c
and P1.1.d respectively.

Per OpenClaw's provider docs, ``"OpenClaw-style Claude CLI usage is allowed
again"`` per Anthropic staff communication. CAQRS adopts the same path:
detect a logged-in ``claude`` CLI on the host, read its credentials, and
issue Anthropic Messages API calls under the user's subscription.
"""

from pathlib import Path

from pydantic import BaseModel

from caqrs.providers._cli_creds import load_claude_cli_cred
from caqrs.providers.types import CompletionResult, Message


class AnthropicViaClaudeCLI:
    """Subscription-backed Anthropic provider reusing local Claude CLI credentials."""

    provider_id: str

    def __init__(
        self,
        *,
        model: str = "claude-opus-4-7",
        home_dir: Path | None = None,
    ) -> None:
        self.provider_id = f"anthropic-cli/{model}"
        self._model = model
        self._home_dir = home_dir

    def is_configured(self) -> bool:
        """True iff a valid Claude CLI cred is reachable (file or Keychain)."""
        return load_claude_cli_cred(self._home_dir) is not None

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
            "AnthropicViaClaudeCLI: HTTP transport lands in P1.1.c. "
            "Use OpenAICompatProvider with a LiteLLM gateway in the meantime.",
        )
