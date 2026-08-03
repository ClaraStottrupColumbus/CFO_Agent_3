# Tests for tasks.compute_next_run — the pure schedule math behind the
# TaskScheduler. All wall-clock cases pass tz=UTC so results are deterministic
# regardless of the machine's timezone.

from datetime import datetime, timedelta, timezone

from app.tasks import compute_next_run

UTC = timezone.utc


def _task(schedule, enabled=True, last_run_utc=None):
    return {"enabled": enabled, "schedule": schedule, "last_run_utc": last_run_utc}


def test_disabled_task_has_no_next_run():
    t = _task({"mode": "daily", "time": "07:00"}, enabled=False)
    assert compute_next_run(t, now=datetime(2026, 7, 17, 12, 0, tzinfo=UTC), tz=UTC) is None


def test_interval_counts_from_last_run():
    t = _task({"mode": "interval", "interval_seconds": 300},
              last_run_utc="2026-07-17T10:00:00+00:00")
    now = datetime(2026, 7, 17, 10, 2, tzinfo=UTC)
    assert compute_next_run(t, now=now) == datetime(2026, 7, 17, 10, 5, tzinfo=UTC)


def test_interval_never_run_counts_from_now():
    t = _task({"mode": "interval", "interval_seconds": 60})
    now = datetime(2026, 7, 17, 10, 0, tzinfo=UTC)
    assert compute_next_run(t, now=now) == now + timedelta(seconds=60)


def test_interval_restart_does_not_replay_overdue_run():
    # last run long before the scheduler (re)started → count from session start,
    # not from the stale last run.
    t = _task({"mode": "interval", "interval_seconds": 300},
              last_run_utc="2026-07-16T08:00:00+00:00")
    now = datetime(2026, 7, 17, 10, 0, 30, tzinfo=UTC)
    session_start = datetime(2026, 7, 17, 10, 0, tzinfo=UTC)
    assert compute_next_run(t, now=now, session_start=session_start) \
        == session_start + timedelta(seconds=300)


def test_interval_new_task_waits_a_full_interval():
    # A task created mid-session must not fire immediately: its first run is
    # created_at + interval, even when the scheduler started long before.
    created = datetime(2026, 7, 17, 11, 0, tzinfo=UTC)
    t = _task({"mode": "interval", "interval_seconds": 300})
    t["created_at"] = created.timestamp()
    session_start = datetime(2026, 7, 17, 9, 0, tzinfo=UTC)
    now = created + timedelta(seconds=5)
    assert compute_next_run(t, now=now, session_start=session_start) \
        == created + timedelta(seconds=300)


def test_interval_floor_is_ten_seconds():
    t = _task({"mode": "interval", "interval_seconds": 1})
    now = datetime(2026, 7, 17, 10, 0, tzinfo=UTC)
    assert compute_next_run(t, now=now) == now + timedelta(seconds=10)


def test_daily_before_time_is_today():
    t = _task({"mode": "daily", "time": "07:30"})
    now = datetime(2026, 7, 17, 6, 0, tzinfo=UTC)
    assert compute_next_run(t, now=now, tz=UTC) == datetime(2026, 7, 17, 7, 30, tzinfo=UTC)


def test_daily_after_time_rolls_to_tomorrow():
    t = _task({"mode": "daily", "time": "07:30"})
    now = datetime(2026, 7, 17, 8, 0, tzinfo=UTC)
    assert compute_next_run(t, now=now, tz=UTC) == datetime(2026, 7, 18, 7, 30, tzinfo=UTC)


def test_daily_exactly_at_time_rolls_forward():
    # An occurrence is strictly in the future, so a just-fired task doesn't refire.
    t = _task({"mode": "daily", "time": "07:30"})
    now = datetime(2026, 7, 17, 7, 30, tzinfo=UTC)
    assert compute_next_run(t, now=now, tz=UTC) == datetime(2026, 7, 18, 7, 30, tzinfo=UTC)


def test_weekly_same_week_and_wrap():
    # 2026-07-17 is a Friday (weekday 4).
    monday = _task({"mode": "weekly", "day_of_week": 0, "time": "09:00"})
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    assert compute_next_run(monday, now=now, tz=UTC) == datetime(2026, 7, 20, 9, 0, tzinfo=UTC)

    saturday = _task({"mode": "weekly", "day_of_week": 5, "time": "09:00"})
    assert compute_next_run(saturday, now=now, tz=UTC) == datetime(2026, 7, 18, 9, 0, tzinfo=UTC)


def test_weekly_same_day_past_time_wraps_a_week():
    friday = _task({"mode": "weekly", "day_of_week": 4, "time": "09:00"})
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)   # Friday after 09:00
    assert compute_next_run(friday, now=now, tz=UTC) == datetime(2026, 7, 24, 9, 0, tzinfo=UTC)


def test_monthly_clamps_to_month_length():
    t = _task({"mode": "monthly", "day_of_month": 31, "time": "07:30"})
    # Jan 31 07:30 already passed → February, clamped to the 28th (2026 is not a leap year).
    now = datetime(2026, 1, 31, 8, 0, tzinfo=UTC)
    assert compute_next_run(t, now=now, tz=UTC) == datetime(2026, 2, 28, 7, 30, tzinfo=UTC)


def test_monthly_december_wraps_to_january():
    t = _task({"mode": "monthly", "day_of_month": 1, "time": "07:00"})
    now = datetime(2026, 12, 15, 12, 0, tzinfo=UTC)
    assert compute_next_run(t, now=now, tz=UTC) == datetime(2027, 1, 1, 7, 0, tzinfo=UTC)
