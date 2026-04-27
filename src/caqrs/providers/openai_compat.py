"""Generic OpenAI-compatible provider (LiteLLM gateway, vLLM, LM Studio, Ollama).

Status: P1.0 stub. Real implementation lands in P1.1.5: httpx async +
``/chat/completions`` with tool-call structured output, mapped from
JSON-schema derived from the requested pydantic schema.

This provider does **not** handle subscription credentials. It expects an
explicit ``base_url`` and ``api_key`` (the LiteLLM gateway issues its own
key, distinct from upstream provider keys).
"""

from pydantic import BaseModel

from caqrs.providers.types import CompletionResult, Message


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
        del messages, schema, max_output_tokens, temperature  # P1.0 stub
        raise NotImplementedError(
            "OpenAICompatProvider: real httpx-based implementation lands in P1.1.5. "
            "Until then, this stub validates the protocol shape only.",
        )
