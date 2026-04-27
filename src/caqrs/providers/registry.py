"""Ordered fallback chain across LLMProvider implementations.

Behaviour matches Mercury's ``src/providers/registry.ts``:

- Try the last-successful provider first (warm path).
- On ``ProviderError`` from any provider, fall through to the next.
- Cache the index of the last successful provider for the next call.

Non-``ProviderError`` exceptions propagate unchanged.
"""

from pydantic import BaseModel

from caqrs.providers.base import LLMProvider
from caqrs.providers.errors import ProviderError
from caqrs.providers.types import CompletionResult, Message


class ProviderRegistry:
    """Ordered fallback registry over a non-empty tuple of providers."""

    def __init__(self, providers: tuple[LLMProvider, ...]) -> None:
        if not providers:
            raise ValueError("ProviderRegistry requires at least one provider.")
        self._providers = providers
        self._last_successful_idx = 0

    @property
    def provider_ids(self) -> tuple[str, ...]:
        return tuple(p.provider_id for p in self._providers)

    @property
    def last_successful_provider_id(self) -> str:
        return self._providers[self._last_successful_idx].provider_id

    def _ordered_indices(self) -> tuple[int, ...]:
        warm = self._last_successful_idx
        rest = tuple(i for i in range(len(self._providers)) if i != warm)
        return (warm, *rest)

    async def complete[T: BaseModel](
        self,
        *,
        messages: tuple[Message, ...],
        schema: type[T],
        max_output_tokens: int,
        temperature: float = 0.0,
    ) -> CompletionResult[T]:
        last_err: ProviderError | None = None
        for idx in self._ordered_indices():
            try:
                result = await self._providers[idx].complete(
                    messages=messages,
                    schema=schema,
                    max_output_tokens=max_output_tokens,
                    temperature=temperature,
                )
            except ProviderError as exc:
                last_err = exc
                continue
            self._last_successful_idx = idx
            return result
        assert last_err is not None  # pragma: no cover (only reachable via ProviderError)
        raise last_err
