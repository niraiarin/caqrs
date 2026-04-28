"""P3.d-3 — LossBudgetTracker.

Caller-side projection that feeds
:attr:`PolicyGatewayConfig.daily_realized_loss_usd`. Reads a broker's
cumulative ``realized_pnl_usd`` (signed, accrued since broker
construction), snapshots the start-of-day baseline, and returns the
day's loss as a non-negative magnitude.

Per ADR-0005 §"Day-boundary semantics" (binding):

- Caller drives the day boundary via :meth:`mark_start_of_day`.
- ``today`` is a tz-aware ``date`` derived from the active
  ResearchPlan / WalkForwardWindow timezone (UTC default).
- The tracker holds only a single ``Decimal`` baseline + the current
  day; never a broker reference, never a clock.

API contract details (resolving iter-2 empirical ambiguities):

- Same-day :meth:`mark_start_of_day` is **idempotent** — re-snapshotting
  mid-day would erase intra-day losses, defeating the kill switch.
- Different-day :meth:`mark_start_of_day` **rebases** — yesterday's
  losses are absorbed into history; only today's incremental movement
  counts toward the budget.
- :meth:`today_realized_loss_usd` raises ``RuntimeError`` until the
  first :meth:`mark_start_of_day` — silent zero would mean the loss
  budget is effectively disabled, which is the worst possible failure
  mode for a kill switch.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from caqrs.execution.paper_broker import PaperBroker
from caqrs.policy.gateway import PolicyGatewayConfig
from caqrs.policy.loss_budget_tracker import (
    LossBudgetTracker,
    RealizedPnLSource,
)


class _StubSource:
    """Minimal :class:`RealizedPnLSource` for tests."""

    def __init__(self, realized_pnl_usd: Decimal) -> None:
        self.realized_pnl_usd = realized_pnl_usd


def test_realized_pnl_source_protocol_matches_paper_broker() -> None:
    """The PaperBroker satisfies the Protocol structurally — the tracker
    works against any broker exposing realized_pnl_usd."""
    broker = PaperBroker(initial_capital_usd=Decimal("100000"))
    src: RealizedPnLSource = broker  # structural check — must compile
    assert src.realized_pnl_usd == Decimal(0)


# === Pre-mark behaviour ===


def test_today_realized_loss_raises_before_first_mark() -> None:
    """Silent zero would disable the kill switch. Raise loudly."""
    tracker = LossBudgetTracker()
    src = _StubSource(realized_pnl_usd=Decimal("-500"))
    with pytest.raises(RuntimeError, match="mark_start_of_day"):
        tracker.today_realized_loss_usd(src)


# === Mark + read ===


def test_zero_loss_when_pnl_unchanged_since_baseline() -> None:
    tracker = LossBudgetTracker()
    src = _StubSource(realized_pnl_usd=Decimal("0"))
    tracker.mark_start_of_day(today=date(2026, 4, 28), source=src)
    assert tracker.today_realized_loss_usd(src) == Decimal(0)


def test_zero_loss_when_pnl_rises_since_baseline() -> None:
    """Net-positive day → magnitude is 0 (PolicyGatewayConfig field is
    a non-negative magnitude)."""
    tracker = LossBudgetTracker()
    src = _StubSource(realized_pnl_usd=Decimal("0"))
    tracker.mark_start_of_day(today=date(2026, 4, 28), source=src)
    src.realized_pnl_usd = Decimal("250")  # gain since baseline
    assert tracker.today_realized_loss_usd(src) == Decimal(0)


def test_positive_loss_magnitude_when_pnl_falls_since_baseline() -> None:
    """Net-negative day → magnitude is the positive shortfall."""
    tracker = LossBudgetTracker()
    src = _StubSource(realized_pnl_usd=Decimal("1000"))
    tracker.mark_start_of_day(today=date(2026, 4, 28), source=src)
    src.realized_pnl_usd = Decimal("700")  # 300 lost since baseline
    assert tracker.today_realized_loss_usd(src) == Decimal("300")


def test_works_against_already_negative_baseline() -> None:
    """Cumulative PnL below zero at start of day is fine — only the
    delta from baseline matters."""
    tracker = LossBudgetTracker()
    src = _StubSource(realized_pnl_usd=Decimal("-2000"))
    tracker.mark_start_of_day(today=date(2026, 4, 28), source=src)
    src.realized_pnl_usd = Decimal("-2300")  # 300 more lost today
    assert tracker.today_realized_loss_usd(src) == Decimal("300")


# === Same-day idempotency (intra-day re-mark must NOT erase losses) ===


def test_same_day_mark_is_idempotent() -> None:
    """Re-snapshotting mid-day would erase the day's accrued loss and
    silently disable the kill switch. Same-date calls are no-ops."""
    tracker = LossBudgetTracker()
    src = _StubSource(realized_pnl_usd=Decimal("1000"))
    tracker.mark_start_of_day(today=date(2026, 4, 28), source=src)
    src.realized_pnl_usd = Decimal("700")  # 300 down
    # Operator accidentally calls mark_start_of_day again same day.
    tracker.mark_start_of_day(today=date(2026, 4, 28), source=src)
    # Still reports today's loss — baseline NOT re-snapshot.
    assert tracker.today_realized_loss_usd(src) == Decimal("300")


# === Different-day rebase ===


def test_different_day_mark_rebases_baseline() -> None:
    """Yesterday's losses absorbed into history; today starts fresh."""
    tracker = LossBudgetTracker()
    src = _StubSource(realized_pnl_usd=Decimal("1000"))
    tracker.mark_start_of_day(today=date(2026, 4, 28), source=src)
    src.realized_pnl_usd = Decimal("400")  # 600 lost on day 1
    assert tracker.today_realized_loss_usd(src) == Decimal("600")
    # Next trading day — caller marks start-of-day again.
    tracker.mark_start_of_day(today=date(2026, 4, 29), source=src)
    # Yesterday's loss no longer counts toward today.
    assert tracker.today_realized_loss_usd(src) == Decimal(0)
    # Now lose another 100 on day 2.
    src.realized_pnl_usd = Decimal("300")
    assert tracker.today_realized_loss_usd(src) == Decimal("100")


def test_rebase_resets_loss_even_after_intraday_drawdown() -> None:
    """Confirms that "same-day idempotent" doesn't accidentally apply
    when the date strictly advances."""
    tracker = LossBudgetTracker()
    src = _StubSource(realized_pnl_usd=Decimal("0"))
    tracker.mark_start_of_day(today=date(2026, 4, 28), source=src)
    src.realized_pnl_usd = Decimal("-1500")
    assert tracker.today_realized_loss_usd(src) == Decimal("1500")
    tracker.mark_start_of_day(today=date(2026, 4, 29), source=src)
    assert tracker.today_realized_loss_usd(src) == Decimal(0)


# === Backwards date is rejected (operational sanity) ===


def test_backwards_day_mark_raises() -> None:
    """Calling mark_start_of_day with an earlier date than the current
    baseline-day is a caller bug (clock drift, wrong TZ). Raise loudly."""
    tracker = LossBudgetTracker()
    src = _StubSource(realized_pnl_usd=Decimal("0"))
    tracker.mark_start_of_day(today=date(2026, 4, 28), source=src)
    with pytest.raises(ValueError, match="earlier than"):
        tracker.mark_start_of_day(today=date(2026, 4, 27), source=src)


# === Output type is exactly what PolicyGatewayConfig expects ===


def test_today_realized_loss_returns_decimal() -> None:
    """The return value drops directly into
    PolicyGatewayConfig.daily_realized_loss_usd: Decimal = Field(ge=0)."""
    tracker = LossBudgetTracker()
    src = _StubSource(realized_pnl_usd=Decimal("0"))
    tracker.mark_start_of_day(today=date(2026, 4, 28), source=src)
    src.realized_pnl_usd = Decimal("-750")
    loss = tracker.today_realized_loss_usd(src)
    assert isinstance(loss, Decimal)
    # Construct config without conversion — proves the contract.
    cfg = PolicyGatewayConfig(daily_realized_loss_usd=loss)
    assert cfg.daily_realized_loss_usd == Decimal("750")
