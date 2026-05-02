"""DailyQuotaTracker — persistent counter for EDINET DB free-plan
100 req/day budget.

The tracker is the second half of the quota-management story (after
the TTL cache shipped in the previous slice): the cache eliminates
*repeated* requests, the tracker enforces a hard ceiling on the
*absolute* count per UTC day. Survives process restarts because the
log lives in SQLite.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from caqrs.data.edinetdb.quota import (
    DailyQuotaTracker,
    EdinetDbQuotaExhaustedError,
)

# === Construction ===


def test_tracker_creates_db_lazily(tmp_path: Path) -> None:
    db = tmp_path / "quota.db"
    tracker = DailyQuotaTracker(db_path=db, daily_cap=100)
    assert not db.exists()
    tracker.record_request(now=datetime(2026, 4, 28, 12, 0, tzinfo=UTC))
    assert db.exists()


def test_default_daily_cap_is_100() -> None:
    """Default cap matches the EDINET DB free plan."""
    tracker = DailyQuotaTracker(db_path=Path("/tmp/_unused"))
    assert tracker.daily_cap == 100


# === Counting ===


def test_zero_count_on_fresh_tracker(tmp_path: Path) -> None:
    tracker = DailyQuotaTracker(db_path=tmp_path / "q.db", daily_cap=100)
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    assert tracker.requests_today(now=now) == 0
    assert tracker.quota_remaining(now=now) == 100


@pytest.mark.traces("DATA-EDB-A1")
def test_record_request_increments_count(tmp_path: Path) -> None:
    tracker = DailyQuotaTracker(db_path=tmp_path / "q.db", daily_cap=100)
    base = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    tracker.record_request(now=base)
    tracker.record_request(now=base)
    tracker.record_request(now=base)
    assert tracker.requests_today(now=base) == 3
    assert tracker.quota_remaining(now=base) == 97


# === UTC day boundary ===


def test_yesterday_requests_dont_count_toward_today(tmp_path: Path) -> None:
    tracker = DailyQuotaTracker(db_path=tmp_path / "q.db", daily_cap=100)
    yesterday = datetime(2026, 4, 27, 23, 30, tzinfo=UTC)
    today = datetime(2026, 4, 28, 0, 30, tzinfo=UTC)
    for _ in range(50):
        tracker.record_request(now=yesterday)
    # Today's count starts fresh.
    assert tracker.requests_today(now=today) == 0
    assert tracker.quota_remaining(now=today) == 100


def test_late_night_request_belongs_to_calling_day(tmp_path: Path) -> None:
    """A request at 23:59:59 UTC counts for that calendar day; the
    next request at 00:00:01 UTC the following day starts the new
    day's bucket. Critical for callers who run a daily cron near
    midnight."""
    tracker = DailyQuotaTracker(db_path=tmp_path / "q.db", daily_cap=100)
    night = datetime(2026, 4, 27, 23, 59, 59, tzinfo=UTC)
    morning = datetime(2026, 4, 28, 0, 0, 1, tzinfo=UTC)
    tracker.record_request(now=night)
    assert tracker.requests_today(now=night) == 1
    assert tracker.requests_today(now=morning) == 0


# === Exhaustion guard ===


def test_assert_quota_available_passes_below_cap(tmp_path: Path) -> None:
    tracker = DailyQuotaTracker(db_path=tmp_path / "q.db", daily_cap=5)
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    for _ in range(4):
        tracker.record_request(now=now)
    # Still 1 budget left → no raise.
    tracker.assert_quota_available(now=now)


def test_assert_quota_available_raises_at_cap(tmp_path: Path) -> None:
    tracker = DailyQuotaTracker(db_path=tmp_path / "q.db", daily_cap=5)
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    for _ in range(5):
        tracker.record_request(now=now)
    with pytest.raises(EdinetDbQuotaExhaustedError, match="5"):
        tracker.assert_quota_available(now=now)


def test_quota_resets_at_utc_midnight(tmp_path: Path) -> None:
    tracker = DailyQuotaTracker(db_path=tmp_path / "q.db", daily_cap=5)
    yesterday = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    today = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    for _ in range(5):
        tracker.record_request(now=yesterday)
    # Yesterday was full. Today is empty.
    tracker.assert_quota_available(now=today)
    assert tracker.quota_remaining(now=today) == 5


# === Persistence ===


def test_log_persists_across_instances(tmp_path: Path) -> None:
    """Process restart preserves today's count — the whole point of
    using SQLite over an in-memory counter."""
    db = tmp_path / "shared.db"
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)

    writer = DailyQuotaTracker(db_path=db, daily_cap=100)
    for _ in range(7):
        writer.record_request(now=now)

    reader = DailyQuotaTracker(db_path=db, daily_cap=100)
    assert reader.requests_today(now=now) == 7


# === Maintenance ===


def test_purge_old_keeps_only_recent_days(tmp_path: Path) -> None:
    """Long-lived deployments accumulate millions of timestamps over
    months; purge_old() trims to a configurable retention window."""
    tracker = DailyQuotaTracker(db_path=tmp_path / "q.db", daily_cap=100)
    old = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    recent = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    for _ in range(50):
        tracker.record_request(now=old)
    for _ in range(3):
        tracker.record_request(now=recent)
    # Purge anything older than 30 days from `recent`.
    tracker.purge_older_than(cutoff=datetime(2026, 3, 29, 0, 0, tzinfo=UTC))
    # Only the 3 recent rows remain.
    assert tracker.requests_today(now=recent) == 3
    # And yesterday-of-old (rolled back further) shows 0 because the
    # rows have been purged.
    assert tracker.requests_today(now=old) == 0


# === Custom cap ===


def test_custom_daily_cap_respected(tmp_path: Path) -> None:
    """Paid plans (or self-imposed safety margins) can override the
    100 default."""
    tracker = DailyQuotaTracker(db_path=tmp_path / "q.db", daily_cap=10_000)
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    for _ in range(5_000):
        tracker.record_request(now=now)
    assert tracker.quota_remaining(now=now) == 5_000
