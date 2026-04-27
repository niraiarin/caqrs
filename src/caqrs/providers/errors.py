"""Provider error hierarchy.

The orchestrator catches ``ProviderError`` to drive fallback. Specific
subclasses let policy code distinguish auth failures (require human
intervention) from transient network/rate issues (retry / fall through).

``SchemaViolationError`` indicates the model returned text that does not
parse against the requested pydantic schema; the registry falls through to
the next provider rather than raising, on the assumption that a different
model may produce schema-compliant output.
"""


class ProviderError(Exception):
    """Base for all provider-layer failures."""


class AuthError(ProviderError):
    """Credentials missing, expired, or rejected."""


class RateLimitError(ProviderError):
    """Quota or per-minute limit exceeded."""


class NetworkError(ProviderError):
    """Transport-level failure (DNS, TLS, timeout, connection reset)."""


class ParseError(ProviderError):
    """Provider returned malformed JSON or non-text content."""


class SchemaViolationError(ProviderError):
    """Output parsed but failed pydantic schema validation."""
