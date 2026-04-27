"""Auditor-agent output artifact.

The Auditor receives a ``HypothesisCard`` (acceptance criteria) plus
a ``BacktestReport`` (actual results) and decides whether the
strategy may be promoted to a ``StrategyDecision``. Its output is
``AuditReport`` with a per-criterion pass/fail trace.
"""

from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from caqrs.schemas.common import RunId, RunMetadata, StrictBaseModel


class AuditVerdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"


class AcceptanceCheck(StrictBaseModel):
    """One acceptance criterion evaluated against a backtest metric."""

    metric_path: str = Field(min_length=1, max_length=200)
    op: str = Field(pattern=r"^(>=|<=|>|<|==|!=)$")
    threshold: Decimal
    actual: Decimal | None = None
    passed: bool


class AuditReport(StrictBaseModel):
    """Output of the Auditor agent. Per-criterion verdict trace."""

    schema_version: int = Field(default=1, ge=1, le=1)
    metadata: RunMetadata
    hypothesis_run_id: RunId
    backtest_run_id: RunId

    verdict: AuditVerdict
    checks: tuple[AcceptanceCheck, ...] = Field(min_length=1, max_length=20)
    rationale: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def _verdict_matches_checks(self) -> Self:
        """A PASS verdict requires every check to have ``passed=True``;
        a FAIL verdict requires at least one ``passed=False``."""
        all_passed = all(check.passed for check in self.checks)
        if self.verdict is AuditVerdict.PASS and not all_passed:
            raise ValueError(
                "verdict=pass requires all acceptance checks to pass; "
                f"{sum(1 for c in self.checks if not c.passed)} failed.",
            )
        if self.verdict is AuditVerdict.FAIL and all_passed:
            raise ValueError(
                "verdict=fail requires at least one acceptance check to fail; all checks passed.",
            )
        return self
