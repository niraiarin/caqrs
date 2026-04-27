"""ProviderRegistry fallback behaviour tests."""

import asyncio
from decimal import Decimal

import pytest
from pydantic import BaseModel

from caqrs.providers import (
    CompletionResult,
    Message,
    ProviderRegistry,
    ProviderUsage,
    Role,
)
from caqrs.providers.errors import AuthError, NetworkError, RateLimitError


class _Out(BaseModel):
    answer: str


class _FakeProvider:
    """Deterministic test double matching the LLMProvider protocol."""

    provider_id: str

    def __init__(
        self,
        *,
        provider_id: str,
        answer: str | None = None,
        raise_first_n: int = 0,
        exc: Exception | None = None,
    ) -> None:
        self.provider_id = provider_id
        self._answer = answer
        self._raise_first_n = raise_first_n
        self._exc = exc
        self.call_count = 0

    async def complete[T: BaseModel](
        self,
        *,
        messages: tuple[Message, ...],
        schema: type[T],
        max_output_tokens: int,
        temperature: float = 0.0,
    ) -> CompletionResult[T]:
        del messages, max_output_tokens, temperature
        self.call_count += 1
        if self._raise_first_n > 0 and self._exc is not None:
            self._raise_first_n -= 1
            raise self._exc
        if self._answer is None:
            raise AssertionError(f"{self.provider_id} configured with no answer")
        return CompletionResult[T](
            output=schema(answer=self._answer),
            usage=ProviderUsage(
                token_in=1,
                token_out=1,
                latency_ms=1,
                cost_usd=Decimal("0"),
            ),
            provider_id=self.provider_id,
        )


def _msg() -> tuple[Message, ...]:
    return (Message(role=Role.USER, content="hi"),)


def test_registry_requires_non_empty() -> None:
    with pytest.raises(ValueError, match="at least one provider"):
        ProviderRegistry(providers=())


def test_registry_uses_first_provider_when_healthy() -> None:
    primary = _FakeProvider(provider_id="primary", answer="A")
    backup = _FakeProvider(provider_id="backup", answer="B")
    reg = ProviderRegistry((primary, backup))

    result = asyncio.run(
        reg.complete(messages=_msg(), schema=_Out, max_output_tokens=10),
    )
    assert result.output.answer == "A"
    assert result.provider_id == "primary"
    assert primary.call_count == 1
    assert backup.call_count == 0
    assert reg.last_successful_provider_id == "primary"


def test_registry_falls_through_provider_errors() -> None:
    primary = _FakeProvider(
        provider_id="primary",
        raise_first_n=1,
        exc=NetworkError("transient"),
    )
    backup = _FakeProvider(provider_id="backup", answer="B")
    reg = ProviderRegistry((primary, backup))

    result = asyncio.run(
        reg.complete(messages=_msg(), schema=_Out, max_output_tokens=10),
    )
    assert result.output.answer == "B"
    assert result.provider_id == "backup"
    assert primary.call_count == 1
    assert backup.call_count == 1
    assert reg.last_successful_provider_id == "backup"


def test_registry_warm_path_prefers_last_successful() -> None:
    primary = _FakeProvider(
        provider_id="primary",
        raise_first_n=1,
        exc=AuthError("bad creds"),
    )
    backup = _FakeProvider(provider_id="backup", answer="B")
    reg = ProviderRegistry((primary, backup))

    asyncio.run(reg.complete(messages=_msg(), schema=_Out, max_output_tokens=10))
    assert reg.last_successful_provider_id == "backup"

    primary.call_count = 0
    backup.call_count = 0
    asyncio.run(reg.complete(messages=_msg(), schema=_Out, max_output_tokens=10))
    assert backup.call_count == 1
    assert primary.call_count == 0  # warm path skips the failing primary


def test_registry_raises_last_error_when_all_fail() -> None:
    p1 = _FakeProvider(provider_id="p1", raise_first_n=1, exc=NetworkError("x"))
    p2 = _FakeProvider(provider_id="p2", raise_first_n=1, exc=RateLimitError("y"))
    reg = ProviderRegistry((p1, p2))

    with pytest.raises(RateLimitError):
        asyncio.run(reg.complete(messages=_msg(), schema=_Out, max_output_tokens=10))


def test_registry_propagates_non_provider_exception() -> None:
    primary = _FakeProvider(
        provider_id="primary",
        raise_first_n=1,
        exc=RuntimeError("bug"),
    )
    backup = _FakeProvider(provider_id="backup", answer="B")
    reg = ProviderRegistry((primary, backup))

    with pytest.raises(RuntimeError, match="bug"):
        asyncio.run(reg.complete(messages=_msg(), schema=_Out, max_output_tokens=10))
    assert backup.call_count == 0  # non-ProviderError does not fall through
