"""Per-cycle budget guard.

Each research cycle gets a :class:`CycleBudget` (token cap +
wallclock cap). A :class:`BudgetGuard` is the stateful enforcer:
callers feed token consumption from agent invocations into
``consume(token_in=..., token_out=...)`` and ask ``check()`` before
the next LLM call. The guard surfaces breaches as a
:class:`BudgetStatus` and emits a single ``BUDGET_EXCEEDED`` event
into the supplied :class:`EventLog` on the *first* breach (further
breaches stay silent to avoid event-log spam — the cycle runner is
expected to abort on the first non-OK status anyway).

This is the entry-edge guard for the dual-loop discipline (Mercury
survey item 14 / 15): observability for runaway cycles is structural
rather than a control-flow afterthought.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from caqrs.orchestrator.event_log import EventLog
from caqrs.orchestrator.events import budget_exceeded_event


class BudgetStatusKind(StrEnum):
    """Closed enum of budget check outcomes."""

    OK = "ok"
    TOKEN_EXCEEDED = "token_exceeded"
    WALLCLOCK_EXCEEDED = "wallclock_exceeded"


class CycleBudget(BaseModel):
    """Per-cycle token + wallclock caps."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    cycle_id: str = Field(min_length=1, max_length=32)
    token_cap: int = Field(gt=0)
    wallclock_seconds_cap: float = Field(gt=0)


class BudgetStatus(BaseModel):
    """Snapshot of a guard's state at a check point."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    kind: BudgetStatusKind
    tokens_consumed: int = Field(ge=0)
    token_cap: int = Field(gt=0)
    elapsed_seconds: float = Field(ge=0)
    wallclock_seconds_cap: float = Field(gt=0)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class BudgetGuard:
    """Stateful enforcer of a :class:`CycleBudget`.

    Parameters
    ----------
    budget:
        The immutable cap definition.
    event_log:
        Optional log to receive a single ``BUDGET_EXCEEDED`` event
        on the first breach. ``None`` is permitted for unit tests
        and lightweight callers.
    clock:
        Injectable wallclock source for deterministic tests. Default
        is ``datetime.now(UTC)``.
    """

    def __init__(
        self,
        *,
        budget: CycleBudget,
        event_log: EventLog | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._budget = budget
        self._event_log = event_log
        self._clock = clock or _utc_now
        self._started_at = self._clock()
        self._tokens_consumed = 0
        self._exceeded_emitted = False

    @property
    def budget(self) -> CycleBudget:
        return self._budget

    @property
    def tokens_consumed(self) -> int:
        return self._tokens_consumed

    @property
    def elapsed_seconds(self) -> float:
        return (self._clock() - self._started_at).total_seconds()

    def consume(self, *, token_in: int, token_out: int) -> BudgetStatus:
        """Record token usage from one agent invocation; return current status."""
        if token_in < 0 or token_out < 0:
            msg = "token_in and token_out must be non-negative"
            raise ValueError(msg)
        self._tokens_consumed += token_in + token_out
        return self.check()

    def check(self) -> BudgetStatus:
        """Compute current status; emit BUDGET_EXCEEDED event on first breach."""
        elapsed = self.elapsed_seconds
        kind = self._classify(elapsed=elapsed)
        status = BudgetStatus(
            kind=kind,
            tokens_consumed=self._tokens_consumed,
            token_cap=self._budget.token_cap,
            elapsed_seconds=elapsed,
            wallclock_seconds_cap=self._budget.wallclock_seconds_cap,
        )
        self._emit_if_first_breach(status)
        return status

    def _classify(self, *, elapsed: float) -> BudgetStatusKind:
        if self._tokens_consumed > self._budget.token_cap:
            return BudgetStatusKind.TOKEN_EXCEEDED
        if elapsed > self._budget.wallclock_seconds_cap:
            return BudgetStatusKind.WALLCLOCK_EXCEEDED
        return BudgetStatusKind.OK

    def _emit_if_first_breach(self, status: BudgetStatus) -> None:
        if status.kind is BudgetStatusKind.OK:
            return
        if self._exceeded_emitted or self._event_log is None:
            return
        self._exceeded_emitted = True
        if status.kind is BudgetStatusKind.TOKEN_EXCEEDED:
            self._event_log.append(
                budget_exceeded_event(
                    cycle_id=self._budget.cycle_id,
                    budget_kind="token",
                    consumed=status.tokens_consumed,
                    cap=status.token_cap,
                ),
            )
        else:  # WALLCLOCK_EXCEEDED
            self._event_log.append(
                budget_exceeded_event(
                    cycle_id=self._budget.cycle_id,
                    budget_kind="wallclock",
                    consumed=int(status.elapsed_seconds),
                    cap=int(status.wallclock_seconds_cap),
                ),
            )
