"""Skeptic-agent output artifact.

The Skeptic receives a ``HypothesisCard`` and attempts to falsify it
or surface fatal weaknesses. Its output (``SkepticReport``) decides
whether the cycle proceeds to Research or returns to idle.
"""

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from caqrs.schemas.common import RunId, RunMetadata, StrictBaseModel


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    FATAL = "fatal"


class SkepticVerdict(StrEnum):
    PROCEED = "proceed"
    REQUIRE_REVISION = "require_revision"
    KILL = "kill"


class FalsificationPath(StrictBaseModel):
    """A specific empirical path that would invalidate the hypothesis."""

    description: str = Field(min_length=1, max_length=500)
    severity: Severity
    evidence_marker: str = Field(min_length=1, max_length=500)


class SkepticReport(StrictBaseModel):
    """Output of the Skeptic agent. Decides whether to proceed."""

    schema_version: int = Field(default=1, ge=1, le=1)
    metadata: RunMetadata
    hypothesis_run_id: RunId

    verdict: SkepticVerdict
    falsification_paths: tuple[FalsificationPath, ...] = Field(default=(), max_length=20)
    concerns: tuple[str, ...] = Field(default=(), max_length=20)
    summary: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def _kill_requires_fatal_path(self) -> Self:
        """A KILL verdict must be backed by at least one FATAL falsification path
        OR at least one explicit concern; otherwise the verdict is ungrounded."""
        if self.verdict is not SkepticVerdict.KILL:
            return self
        has_fatal = any(p.severity is Severity.FATAL for p in self.falsification_paths)
        has_concern = bool(self.concerns)
        if not has_fatal and not has_concern:
            raise ValueError(
                "verdict=kill requires at least one FATAL falsification_path "
                "or at least one concern.",
            )
        return self

    @model_validator(mode="after")
    def _proceed_does_not_carry_fatal(self) -> Self:
        """A PROCEED verdict cannot coexist with a fatal falsification path."""
        if self.verdict is SkepticVerdict.PROCEED and any(
            p.severity is Severity.FATAL for p in self.falsification_paths
        ):
            raise ValueError(
                "verdict=proceed is inconsistent with a fatal falsification_path; "
                "downgrade severity or change verdict.",
            )
        return self
