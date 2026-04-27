"""Base class for LLM-backed agents.

Concrete agents (Observer, Hypothesis, Skeptic, Research, Auditor) inherit
from ``LLMAgent`` and fill in their role description, input/output schemas,
and emit-tool description. The base wraps the common pattern:

1. Build the system prompt via ``build_agent_system_prompt`` (role +
   ``RESEARCH_GUARDRAILS`` + emit-tool description).
2. Build the user message from the typed input artifact.
3. Call ``provider.complete`` with the output schema (which the provider
   converts into a tool definition appropriate for its API surface).
4. Wrap the resulting ``CompletionResult[O]`` in an ``AgentResult[O]``,
   converting ``ProviderError`` failures into ``AgentResult`` with
   ``error`` set so the orchestrator can fall through cleanly.
"""

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel

from caqrs.agents.prompts import build_agent_system_prompt
from caqrs.agents.protocol import AgentResult
from caqrs.providers.base import LLMProvider
from caqrs.providers.errors import ProviderError
from caqrs.providers.types import Message, Role
from caqrs.schemas.common import RunMetadata, new_run_id


class LLMAgent[I: BaseModel, O: BaseModel]:
    """Convenience base for LLM-backed agents.

    Subclasses fill the class-level attributes: ``name``, ``role``,
    ``role_brief``, ``input_schema``, ``output_schema``,
    ``emit_tool_description``. They may override ``build_user_message``
    if the default JSON dump of the input is not appropriate.

    The schema attributes are deliberately typed as ``type[I]`` /
    ``type[O]`` so mypy carries the binding from a subclass declaration
    like ``class FooAgent(LLMAgent[FooInput, FooOutput])`` through to
    the provider call and back into the ``AgentResult[O]`` wrapper.
    """

    name: str
    role: str
    role_brief: str
    emit_tool_description: str
    input_schema: type[I]
    output_schema: type[O]

    def __init__(
        self,
        *,
        provider: LLMProvider,
        max_output_tokens: int = 2048,
        temperature: float = 0.0,
        parent_run_id: str | None = None,
    ) -> None:
        self._provider = provider
        self._max_output_tokens = max_output_tokens
        self._temperature = temperature
        self._parent_run_id = parent_run_id

    @property
    def emit_tool_name(self) -> str:
        return f"emit_{self.output_schema.__name__}"

    @property
    def system_prompt(self) -> str:
        return build_agent_system_prompt(
            role=self.role,
            role_brief=self.role_brief,
            emit_tool_name=self.emit_tool_name,
            emit_tool_description=self.emit_tool_description,
        )

    def build_user_message(self, payload: I) -> str:
        """Format the typed input artifact into a user message.

        The default emits an indented JSON dump. Subclasses can override
        when a more compact or domain-specific framing helps the model.
        """
        return payload.model_dump_json(indent=2)

    async def run(self, payload: I, /) -> AgentResult[O]:
        if not isinstance(payload, self.input_schema):
            raise TypeError(
                f"{type(self).__name__}.run expected {self.input_schema.__name__}; "
                f"got {type(payload).__name__}",
            )

        messages = (
            Message(role=Role.SYSTEM, content=self.system_prompt),
            Message(role=Role.USER, content=self.build_user_message(payload)),
        )

        try:
            result = await self._provider.complete(
                messages=messages,
                schema=self.output_schema,
                max_output_tokens=self._max_output_tokens,
                temperature=self._temperature,
            )
        except ProviderError as exc:
            return AgentResult[O](
                output=None,
                error=f"{type(exc).__name__}: {exc}",
                metadata=self._build_run_metadata(provider_id=self._provider.provider_id),
            )

        return AgentResult[O](
            output=result.output,
            error=None,
            metadata=self._build_run_metadata(
                provider_id=result.provider_id,
                token_in=result.usage.token_in,
                token_out=result.usage.token_out,
                latency_ms=result.usage.latency_ms,
                cost_usd=result.usage.cost_usd,
            ),
        )

    def _build_run_metadata(
        self,
        *,
        provider_id: str,
        token_in: int = 0,
        token_out: int = 0,
        latency_ms: int = 0,
        cost_usd: Decimal = Decimal(0),
    ) -> RunMetadata:
        return RunMetadata(
            run_id=new_run_id(),
            parent_id=self._parent_run_id,
            agent_name=self.name,
            model_id=provider_id,
            created_at=datetime.now(UTC),
            llm_cost_usd=cost_usd,
            latency_ms=latency_ms,
            token_in=token_in,
            token_out=token_out,
        )
