from datetime import date, datetime, time, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from jarvis.core.types import Nanos
from jarvis.timeengine.calendar import (
    TRADING_DAY_TZ,
    is_weekend_gap,
    trading_day,
    trading_day_bounds,
    trading_week,
)
from jarvis.timeengine.convert import NS_PER_SECOND, from_utc_ns, local_to_utc_ns, to_utc_ns


def _ny_ns(d: date, h: int, m: int = 0, s: int = 0) -> Nanos:
    return local_to_utc_ns(d, time(h, m, s), TRADING_DAY_TZ, "later")


def _utc_ns(y: int, mo: int, d: int, h: int = 0, m: int = 0) -> Nanos:
    return to_utc_ns(datetime(y, mo, d, h, m, tzinfo=timezone.utc))


# T6 -------------------------------------------------------------------


def test_trading_day_boundary_moves_with_us_dst():
    ns_before = _ny_ns(date(2023, 3, 11), 17, 0, 0)
    ns_after = _ny_ns(date(2023, 3, 13), 17, 0, 0)

    assert from_utc_ns(ns_before, "UTC").isoformat() == "2023-03-11T22:00:00+00:00"
    assert from_utc_ns(ns_after, "UTC").isoformat() == "2023-03-13T21:00:00+00:00"


# T9 -------------------------------------------------------------------


def test_sunday_1700_ny_begins_next_trading_day():
    ns = _ny_ns(date(2023, 6, 4), 17, 0, 0)
    assert trading_day(ns) == date(2023, 6, 5)


# T10 ------------------------------------------------------------------


def test_friday_1700_ny_is_excluded_half_open():
    # WP-003's T10 restatement claims 2023-06-09 17:00 NY "belongs to
    # trading day 2023-06-12 (the following Monday)". That contradicts the
    # literal mechanical formula given in this same work package's required
    # interfaces (local.date() + 1 day, with no weekend-skipping), which
    # gives 2023-06-10 (a Saturday). The original docs/TECHNICAL_BIBLE_1.md
    # T10 entry makes no claim about which day the instant belongs to at
    # all -- it only asserts exclusion from Friday and states the last
    # included instant, both of which are asserted below and match. Per
    # this package's own "report rather than silently adjust" instruction,
    # implemented per the literal formula; see closing notes.
    ns_at_1700 = _ny_ns(date(2023, 6, 9), 17, 0, 0)
    assert trading_day(ns_at_1700) == date(2023, 6, 10)

    ns_last_instant = _ny_ns(date(2023, 6, 9), 16, 59, 59) + 999_999_999
    assert trading_day(Nanos(ns_last_instant)) == date(2023, 6, 9)


# T14 ------------------------------------------------------------------


_MONOTONIC_START_NS = to_utc_ns(datetime(2007, 1, 1, tzinfo=timezone.utc))
_MONOTONIC_END_NS = to_utc_ns(datetime(2026, 12, 31, tzinfo=timezone.utc))


@given(
    ns_a=st.integers(min_value=_MONOTONIC_START_NS, max_value=_MONOTONIC_END_NS),
    ns_b=st.integers(min_value=_MONOTONIC_START_NS, max_value=_MONOTONIC_END_NS),
)
def test_trading_day_monotonic(ns_a: int, ns_b: int) -> None:
    """Property test: trading_day is monotonic non-decreasing in its input
    across 2007-2026. Closes carried debt D-030a (WP-005 item 0)."""
    lo, hi = sorted((ns_a, ns_b))
    assert trading_day(Nanos(lo)) <= trading_day(Nanos(hi))


# Unnamed required tests ------------------------------------------------


def test_trading_day_bounds_are_half_open_and_contiguous():
    d = date(2023, 6, 12)
    next_d = date(2023, 6, 13)

    start, end = trading_day_bounds(d)
    next_start, next_end = trading_day_bounds(next_d)

    assert start < end
    assert end == next_start


def test_trading_week_anchors_on_wednesday():
    # Trading week Mon 2020-12-28 -> Fri 2021-01-01 straddles the Gregorian
    # year boundary. Every instant within it must yield the same WeekId,
    # derived from that week's Wednesday (2020-12-30).
    expected_iso_year, expected_iso_week, _ = date(2020, 12, 30).isocalendar()

    instants = [
        _ny_ns(date(2020, 12, 28), 9, 0),   # Monday morning
        _ny_ns(date(2020, 12, 30), 12, 0),  # Wednesday itself
        _ny_ns(date(2021, 1, 1), 10, 0),    # Friday, next Gregorian year
    ]
    for ns in instants:
        week = trading_week(ns)
        assert (week.iso_year, week.iso_week) == (expected_iso_year, expected_iso_week)
        assert str(week) == f"{expected_iso_year}-W{expected_iso_week:02d}"


def test_weekend_gap_detection():
    assert is_weekend_gap(_utc_ns(2023, 6, 10, 12, 0)) is True  # Saturday
    assert is_weekend_gap(_utc_ns(2023, 6, 7, 12, 0)) is False  # Wednesday
    assert is_weekend_gap(_ny_ns(date(2023, 6, 9), 16, 59)) is False  # Fri 16:59 NY
    assert is_weekend_gap(_ny_ns(date(2023, 6, 9), 17, 1)) is True  # Fri 17:01 NY
    assert is_weekend_gap(_ny_ns(date(2023, 6, 11), 17, 1)) is False  # Sun 17:01 NY


def test_negative_ns_pre_1970_works_correctly():
    from jarvis.timeengine.convert import local_wall

    ns = to_utc_ns(datetime(1969, 12, 31, 12, 0, tzinfo=timezone.utc))
    assert ns == -43_200_000_000_000
    assert ns < 0

    # NY local is 07:00 EST (before the 17:00 rollover), so trading_day is
    # the unchanged calendar date.
    assert trading_day(Nanos(ns)) == date(1969, 12, 31)

    # Tokyo (no DST, +09:00) is 21:00 the same calendar day -- floor
    # division across the epoch, not truncation toward zero.
    d, t = local_wall(Nanos(ns), "Asia/Tokyo")
    assert (d, t) == (date(1969, 12, 31), time(21, 0, 0))


def test_trading_day_bounds_dst_spring_forward_sunday_is_23_hours():
    # The transition (2023-03-12 02:00 NY) falls inside [Sat 1700, Sun 1700),
    # i.e. trading_day_bounds for the Sunday date label itself.
    start, end = trading_day_bounds(date(2023, 3, 12))
    assert (end - start) == 23 * 3600 * NS_PER_SECOND


def test_trading_day_bounds_dst_fall_back_sunday_is_25_hours():
    start, end = trading_day_bounds(date(2023, 11, 5))
    assert (end - start) == 25 * 3600 * NS_PER_SECOND
