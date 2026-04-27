"""Tests for per-cycle budget guard (token + wallclock)."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from caqrs.orchestrator import (
    BudgetGuard,
    BudgetStatus,
    BudgetStatusKind,
    CycleBudget,
    CycleEventKind,
    EventLog,
    new_cycle_id,
)

# === CycleBudget validation ===


def test_cycle_budget_requires_positive_token_cap() -> None:
    with pytest.raises(ValidationError):
        CycleBudget(cycle_id=new_cycle_id(), token_cap=0, wallclock_seconds_cap=10.0)


def test_cycle_budget_requires_positive_wallclock_cap() -> None:
    with pytest.raises(ValidationError):
        CycleBudget(cycle_id=new_cycle_id(), token_cap=100, wallclock_seconds_cap=0.0)


def test_cycle_budget_is_frozen() -> None:
    budget = CycleBudget(cycle_id=new_cycle_id(), token_cap=100, wallclock_seconds_cap=10.0)
    with pytest.raises(ValidationError, match="frozen"):
        budget.token_cap = 200  # type: ignore[misc]


# === BudgetGuard initial state ===


def test_guard_starts_with_zero_consumption() -> None:
    budget = CycleBudget(cycle_id=new_cycle_id(), token_cap=100, wallclock_seconds_cap=10.0)
    guard = BudgetGuard(budget=budget)
    assert guard.tokens_consumed == 0


def test_guard_initial_check_is_ok() -> None:
    budget = CycleBudget(cycle_id=new_cycle_id(), token_cap=100, wallclock_seconds_cap=10.0)
    guard = BudgetGuard(budget=budget)
    status = guard.check()
    assert status.kind is BudgetStatusKind.OK


# === Consumption ===


def test_consume_accumulates_token_in_and_out() -> None:
    budget = CycleBudget(cycle_id=new_cycle_id(), token_cap=100, wallclock_seconds_cap=10.0)
    guard = BudgetGuard(budget=budget)
    guard.consume(token_in=10, token_out=5)
    assert guard.tokens_consumed == 15
    guard.consume(token_in=3, token_out=2)
    assert guard.tokens_consumed == 20


def test_consume_returns_status() -> None:
    budget = CycleBudget(cycle_id=new_cycle_id(), token_cap=100, wallclock_seconds_cap=10.0)
    guard = BudgetGuard(budget=budget)
    status = guard.consume(token_in=10, token_out=5)
    assert status.kind is BudgetStatusKind.OK
    assert status.tokens_consumed == 15


def test_consume_rejects_negative() -> None:
    budget = CycleBudget(cycle_id=new_cycle_id(), token_cap=100, wallclock_seconds_cap=10.0)
    guard = BudgetGuard(budget=budget)
    with pytest.raises(ValueError, match="non-negative"):
        guard.consume(token_in=-1, token_out=0)


# === Token cap enforcement ===


def test_status_flips_to_token_exceeded_when_over_cap() -> None:
    budget = CycleBudget(cycle_id=new_cycle_id(), token_cap=100, wallclock_seconds_cap=10.0)
    guard = BudgetGuard(budget=budget)
    status = guard.consume(token_in=80, token_out=30)
    assert status.kind is BudgetStatusKind.TOKEN_EXCEEDED
    assert status.tokens_consumed == 110
    assert status.token_cap == 100


def test_status_at_exact_cap_is_ok() -> None:
    """Boundary: exactly hitting the cap is still OK; only strictly over trips."""
    budget = CycleBudget(cycle_id=new_cycle_id(), token_cap=100, wallclock_seconds_cap=10.0)
    guard = BudgetGuard(budget=budget)
    status = guard.consume(token_in=60, token_out=40)
    assert status.kind is BudgetStatusKind.OK


# === Wallclock cap enforcement ===


def test_status_flips_to_wallclock_exceeded() -> None:
    cycle_id = new_cycle_id()
    budget = CycleBudget(cycle_id=cycle_id, token_cap=100, wallclock_seconds_cap=5.0)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    times = iter([base, base + timedelta(seconds=6)])
    guard = BudgetGuard(budget=budget, clock=lambda: next(times))
    status = guard.check()
    assert status.kind is BudgetStatusKind.WALLCLOCK_EXCEEDED
    assert status.elapsed_seconds == pytest.approx(6.0)


def test_token_exceeded_takes_priority_over_wallclock_in_status_kind() -> None:
    cycle_id = new_cycle_id()
    budget = CycleBudget(cycle_id=cycle_id, token_cap=10, wallclock_seconds_cap=1.0)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    times = iter([base, base + timedelta(seconds=10)])
    guard = BudgetGuard(budget=budget, clock=lambda: next(times))
    status = guard.consume(token_in=20, token_out=0)
    assert status.kind is BudgetStatusKind.TOKEN_EXCEEDED


# === Event emission ===


def test_first_token_breach_emits_budget_exceeded_event() -> None:
    cycle_id = new_cycle_id()
    budget = CycleBudget(cycle_id=cycle_id, token_cap=100, wallclock_seconds_cap=10.0)
    log = EventLog()
    guard = BudgetGuard(budget=budget, event_log=log)
    guard.consume(token_in=80, token_out=30)
    events = log.filter_by_kind(CycleEventKind.BUDGET_EXCEEDED)
    assert len(events) == 1
    assert events[0].cycle_id == cycle_id
    assert events[0].payload["budget_kind"] == "token"
    assert events[0].payload["consumed"] == 110
    assert events[0].payload["cap"] == 100


def test_first_wallclock_breach_emits_budget_exceeded_event() -> None:
    cycle_id = new_cycle_id()
    budget = CycleBudget(cycle_id=cycle_id, token_cap=100, wallclock_seconds_cap=5.0)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    times = iter([base, base + timedelta(seconds=6)])
    log = EventLog()
    guard = BudgetGuard(budget=budget, event_log=log, clock=lambda: next(times))
    guard.check()
    events = log.filter_by_kind(CycleEventKind.BUDGET_EXCEEDED)
    assert len(events) == 1
    assert events[0].payload["budget_kind"] == "wallclock"
    assert events[0].payload["cap"] == 5


def test_subsequent_breaches_do_not_re_emit() -> None:
    """Once a breach is reported, the guard stays quiet to avoid event spam."""
    cycle_id = new_cycle_id()
    budget = CycleBudget(cycle_id=cycle_id, token_cap=100, wallclock_seconds_cap=10.0)
    log = EventLog()
    guard = BudgetGuard(budget=budget, event_log=log)
    guard.consume(token_in=80, token_out=30)
    guard.consume(token_in=10, token_out=10)
    guard.check()
    events = log.filter_by_kind(CycleEventKind.BUDGET_EXCEEDED)
    assert len(events) == 1


def test_no_event_log_means_no_emission_but_status_still_correct() -> None:
    budget = CycleBudget(cycle_id=new_cycle_id(), token_cap=100, wallclock_seconds_cap=10.0)
    guard = BudgetGuard(budget=budget)  # no event_log
    status = guard.consume(token_in=200, token_out=0)
    assert status.kind is BudgetStatusKind.TOKEN_EXCEEDED


# === BudgetStatus shape ===


def test_status_carries_full_snapshot() -> None:
    budget = CycleBudget(cycle_id=new_cycle_id(), token_cap=100, wallclock_seconds_cap=10.0)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    times = iter([base, base + timedelta(seconds=2), base + timedelta(seconds=2)])
    guard = BudgetGuard(budget=budget, clock=lambda: next(times))
    guard.consume(token_in=5, token_out=3)
    status = guard.check()
    assert status.tokens_consumed == 8
    assert status.token_cap == 100
    assert status.elapsed_seconds == pytest.approx(2.0)
    assert status.wallclock_seconds_cap == 10.0


def test_status_is_frozen() -> None:
    status = BudgetStatus(
        kind=BudgetStatusKind.OK,
        tokens_consumed=0,
        token_cap=100,
        elapsed_seconds=0.0,
        wallclock_seconds_cap=10.0,
    )
    with pytest.raises(ValidationError, match="frozen"):
        status.kind = BudgetStatusKind.TOKEN_EXCEEDED  # type: ignore[misc]
