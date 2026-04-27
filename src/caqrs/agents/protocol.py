"""Agent protocol: the abstract contract every CAQRS agent satisfies.

An Agent is a typed async function ``Input -> AgentResult[Output]``. Concrete
agents (Observer, Hypothesis, Skeptic, Research, Auditor) match the protocol
structurally; explicit inheritance is not required, which keeps the door open
to lightweight functional adapters.

Implementation-side IO (LLM calls, tool invocation) is encapsulated inside
``run``; the protocol only constrains the type contract.
"""

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from caqrs.schemas.common import RunMetadata


class AgentResult[T: BaseModel](BaseModel):
    """Wrapper around an agent's typed output, plus failure modes the
    orchestrator can react to without raising into the agent loop.

    Either ``output`` or ``error`` is set; ``is_ok`` returns True iff
    ``output is not None and error is None``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    output: T | None = None
    error: str | None = None
    metadata: RunMetadata

    def is_ok(self) -> bool:
        return self.output is not None and self.error is None


@runtime_checkable
class Agent[I: BaseModel, O: BaseModel](Protocol):
    """Structural protocol for any CAQRS agent.

    Implementers do not need to inherit; matching ``name`` and the ``run``
    signature is enough.
    """

    name: str

    async def run(self, payload: I, /) -> AgentResult[O]: ...
