"""Caller-side projection of broker realized PnL onto today's loss
magnitude.

The :class:`LossBudgetTracker` is the missing piece between the
PaperBroker / live broker and the Policy Gateway's
:attr:`PolicyGatewayConfig.daily_realized_loss_usd` constraint. It
takes a broker's cumulative ``realized_pnl_usd`` (signed; positive =
gain, negative = loss, accrued since broker construction), snapshots
the start-of-day baseline, and returns the day's realized loss as a
non-negative magnitude.

Per ADR-0005 §"Day-boundary semantics" (binding for this slice):

- The CycleRunner caller drives day boundaries via
  :meth:`mark_start_of_day` — neither this tracker, the gateway, nor
  the broker discovers "today" on its own.
- ``today`` is a tz-aware :class:`datetime.date` derived from the
  active ResearchPlan / WalkForwardWindow timezone (UTC default).
- The tracker holds only one ``Decimal`` baseline + one ``date``;
  never a broker reference, never a clock.

This file is the only place the projection is implemented; the
gateway itself (``apply_policy_gateway``) stays a pure function and
sees only the resulting ``Decimal`` as a config field — never the
broker, never the tracker.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Protocol


class RealizedPnLSource(Protocol):
    """Structural type: anything exposing ``realized_pnl_usd: Decimal``.

    Both :class:`caqrs.execution.paper_broker.PaperBroker` and the
    eventual P4 live-broker adapter satisfy this protocol by
    construction. The tracker is broker-agnostic — swapping a paper
    broker for a live one requires no tracker change.

    Per ADR-0005, ``realized_pnl_usd`` is **cumulative since broker
    construction** and **signed** (positive = net gain, negative = net
    loss). Day-boundary aggregation is the tracker's job, not the
    broker's.
    """

    @property
    def realized_pnl_usd(self) -> Decimal: ...


class LossBudgetTracker:
    """Snapshot-and-project the day's realized loss for the gateway.

    Lifecycle (the only valid call sequence):

    1. Construct once per CycleRunner (or per supervisor process).
    2. Caller calls :meth:`mark_start_of_day` at cycle 0 of each
       trading day with a tz-aware ``date`` derived from the active
       ResearchPlan window timezone.
    3. Per cycle, caller calls :meth:`today_realized_loss_usd` and
       drops the result into a fresh
       :class:`PolicyGatewayConfig.daily_realized_loss_usd`.

    The tracker does not call :func:`apply_policy_gateway`, does not
    poll, does not retain a reference to ``source`` between calls, and
    does not consult a clock.
    """

    def __init__(self) -> None:
        self._baseline_pnl_usd: Decimal | None = None
        self._current_day: date | None = None

    def mark_start_of_day(self, *, today: date, source: RealizedPnLSource) -> None:
        """Snapshot ``source.realized_pnl_usd`` as the baseline for ``today``.

        Same-day re-call is **idempotent**: re-snapshotting mid-day
        would erase intra-day losses and silently disable the kill
        switch. Different-day call **rebases** to the current
        cumulative figure (yesterday's losses are absorbed into history;
        only today's incremental movement counts toward the budget).

        :raises ValueError: if ``today`` is strictly earlier than the
            current baseline-day (caller bug — clock drift, wrong TZ).
        """
        if self._current_day is not None:
            if today < self._current_day:
                msg = (
                    f"mark_start_of_day got today={today}, which is "
                    f"earlier than current baseline-day {self._current_day}."
                )
                raise ValueError(msg)
            if today == self._current_day:
                # Idempotent — preserve intra-day baseline.
                return
        # Fresh tracker, or strictly-later date: snapshot now.
        self._baseline_pnl_usd = source.realized_pnl_usd
        self._current_day = today

    def today_realized_loss_usd(self, source: RealizedPnLSource) -> Decimal:
        """Return today's realized-loss magnitude (≥ 0).

        Computed as ``max(Decimal(0), baseline - source.realized_pnl_usd)``:

        - net-flat or net-positive day → ``Decimal(0)``
        - net-negative day → positive shortfall

        Result type matches
        :attr:`PolicyGatewayConfig.daily_realized_loss_usd` exactly so
        the caller drops it in without conversion.

        :raises RuntimeError: if :meth:`mark_start_of_day` has never
            been called. Returning a silent zero would mean the loss
            budget is effectively disabled — the worst possible failure
            mode for a kill switch.
        """
        if self._baseline_pnl_usd is None:
            msg = (
                "today_realized_loss_usd called before mark_start_of_day; "
                "the loss-budget kill switch would be silently disabled. "
                "Call mark_start_of_day(today, source) at cycle 0 of each "
                "trading day."
            )
            raise RuntimeError(msg)
        delta = self._baseline_pnl_usd - source.realized_pnl_usd
        return delta if delta > 0 else Decimal(0)
