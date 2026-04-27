"""LLM provider abstraction and concrete providers.

Layout follows OpenClaw's provider plugin model: each concrete provider
encapsulates its own transport, auth, and request shaping behind a single
``LLMProvider`` protocol; the registry orders them and falls through on
``ProviderError``.
"""

from caqrs.providers.anthropic_cli import AnthropicViaClaudeCLI
from caqrs.providers.base import LLMProvider
from caqrs.providers.codex_cli import OpenAIViaCodexCLI
from caqrs.providers.errors import (
    AuthError,
    NetworkError,
    ParseError,
    ProviderError,
    RateLimitError,
    SchemaViolationError,
)
from caqrs.providers.openai_compat import OpenAICompatProvider
from caqrs.providers.registry import ProviderRegistry
from caqrs.providers.types import CompletionResult, Message, ProviderUsage, Role

__all__ = [
    "AnthropicViaClaudeCLI",
    "AuthError",
    "CompletionResult",
    "LLMProvider",
    "Message",
    "NetworkError",
    "OpenAICompatProvider",
    "OpenAIViaCodexCLI",
    "ParseError",
    "ProviderError",
    "ProviderRegistry",
    "ProviderUsage",
    "RateLimitError",
    "Role",
    "SchemaViolationError",
]
