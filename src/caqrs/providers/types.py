"""Provider-layer message and result types.

Mirrors the OpenClaw provider abstraction (``api: "openai-completions" |
"anthropic-messages"``) at the type level: a list of role-tagged messages
plus a pydantic schema, returning a typed completion plus usage bookkeeping.
"""

from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class Message(BaseModel):
    """A single role-tagged message in a conversation."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    role: Role
    content: str = Field(min_length=1, max_length=200_000)


class ProviderUsage(BaseModel):
    """Per-call cost and latency bookkeeping. Feeds RunMetadata."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    token_in: Annotated[int, Field(ge=0)]
    token_out: Annotated[int, Field(ge=0)]
    latency_ms: Annotated[int, Field(ge=0)]
    cost_usd: Annotated[Decimal, Field(ge=0)]


class CompletionResult[T: BaseModel](BaseModel):
    """A typed completion: parsed schema instance + usage + provider id.

    ``provider_id`` is the same shape OpenClaw uses (``provider/model``)
    so artifacts can carry it in ``RunMetadata.model_id`` directly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    output: T
    usage: ProviderUsage
    provider_id: str = Field(min_length=1, max_length=200)
