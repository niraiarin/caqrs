"""Protocol shape test for the Agent interface."""

import asyncio

from pydantic import BaseModel

from caqrs.agents.protocol import Agent, AgentResult
from caqrs.schemas.common import RunMetadata, new_run_id, utc_now


class _Input(BaseModel):
    text: str


class _Output(BaseModel):
    upper: str


class _UpperAgent:
    name: str = "upper"

    async def run(self, payload: _Input, /) -> AgentResult[_Output]:
        return AgentResult[_Output](
            output=_Output(upper=payload.text.upper()),
            error=None,
            metadata=RunMetadata(
                run_id=new_run_id(),
                parent_id=None,
                agent_name=self.name,
                model_id="test",
                created_at=utc_now(),
            ),
        )


def test_agent_protocol_structural_match() -> None:
    a: Agent[_Input, _Output] = _UpperAgent()
    assert isinstance(a, Agent)


def test_agent_returns_typed_result() -> None:
    a = _UpperAgent()
    result = asyncio.run(a.run(_Input(text="hi")))
    assert result.is_ok()
    assert result.output is not None
    assert result.output.upper == "HI"


def test_agent_result_error_path() -> None:
    err = AgentResult[_Output](
        output=None,
        error="something went wrong",
        metadata=RunMetadata(
            run_id=new_run_id(),
            parent_id=None,
            agent_name="x",
            model_id="test",
            created_at=utc_now(),
        ),
    )
    assert not err.is_ok()
