"""Tests for the Heartbeat interval-based fire tracker."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from caqrs.orchestrator import Heartbeat


def _clock_iter(values: list[datetime]) -> Callable[[], datetime]:
    """Build a callable that returns successive datetime values per call.

    pytest.fail when exhausted so a test that miscounts clock reads
    surfaces as a clear assertion rather than StopIteration.
    """
    it = iter(values)

    def _read() -> datetime:
        try:
            return next(it)
        except StopIteration:
            pytest.fail("Heartbeat called clock more times than the test provided values for")

    return _read


# === Initial state ===


def test_heartbeat_is_due_before_first_fire() -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    hb = Heartbeat(interval=timedelta(minutes=5), clock=_clock_iter([base]))
    assert hb.is_due() is True


def test_heartbeat_last_fired_at_is_none_initially() -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    hb = Heartbeat(interval=timedelta(minutes=5), clock=_clock_iter([base]))
    assert hb.last_fired_at is None


# === Fire updates state ===


def test_fire_sets_last_fired_at_to_current_clock() -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    hb = Heartbeat(interval=timedelta(minutes=5), clock=_clock_iter([base]))
    hb.fire()
    assert hb.last_fired_at == base


def test_is_due_false_immediately_after_fire() -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    hb = Heartbeat(
        interval=timedelta(minutes=5),
        clock=_clock_iter([base, base]),  # one for fire, one for is_due
    )
    hb.fire()
    assert hb.is_due() is False


# === Interval elapsed ===


def test_is_due_true_when_interval_exactly_elapsed() -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    hb = Heartbeat(
        interval=timedelta(minutes=5),
        clock=_clock_iter([base, base + timedelta(minutes=5)]),
    )
    hb.fire()
    assert hb.is_due() is True


def test_is_due_false_when_interval_not_yet_elapsed() -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    hb = Heartbeat(
        interval=timedelta(minutes=5),
        clock=_clock_iter([base, base + timedelta(minutes=4)]),
    )
    hb.fire()
    assert hb.is_due() is False


def test_is_due_true_well_past_interval() -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    hb = Heartbeat(
        interval=timedelta(minutes=5),
        clock=_clock_iter([base, base + timedelta(hours=2)]),
    )
    hb.fire()
    assert hb.is_due() is True


def test_consecutive_fires_advance_last_fired_at() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=10)
    hb = Heartbeat(interval=timedelta(minutes=5), clock=_clock_iter([t0, t1]))
    hb.fire()
    assert hb.last_fired_at == t0
    hb.fire()
    assert hb.last_fired_at == t1


# === Validation ===


def test_heartbeat_rejects_zero_interval() -> None:
    with pytest.raises(ValueError, match="interval must be positive"):
        Heartbeat(interval=timedelta(0))


def test_heartbeat_rejects_negative_interval() -> None:
    with pytest.raises(ValueError, match="interval must be positive"):
        Heartbeat(interval=timedelta(seconds=-1))


# === Default clock ===


def test_default_clock_is_utc_now() -> None:
    """Without a clock argument the heartbeat must read tz-aware UTC now."""
    hb = Heartbeat(interval=timedelta(seconds=1))
    hb.fire()
    assert hb.last_fired_at is not None
    assert hb.last_fired_at.tzinfo is not None
