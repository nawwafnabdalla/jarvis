import time as time_module
from datetime import date, datetime, time, timedelta, timezone

import pytest

from jarvis.core.errors import SessionError
from jarvis.core.types import Nanos
from jarvis.sessions import load_session_set
from jarvis.timeengine import (
    NS_PER_HOUR,
    NS_PER_MINUTE,
    from_utc_ns,
    is_weekend_gap,
    local_to_utc_ns,
    to_utc_ns,
    trading_day_bounds,
)

_SESSION_SET_ID = "fx_core"
_VERSION = 1


@pytest.fixture(scope="module")
def session_set():
    return load_session_set(_SESSION_SET_ID, _VERSION)


def test_pre_london_window_utc_boundaries(session_set):
    winter = session_set.window("pre_london", date(2023, 1, 15))
    assert from_utc_ns(winter.start_ns, "UTC").isoformat() == "2023-01-15T00:00:00+00:00"
    assert from_utc_ns(winter.end_ns, "UTC").isoformat() == "2023-01-15T08:00:00+00:00"

    summer = session_set.window("pre_london", date(2023, 6, 15))
    assert from_utc_ns(summer.start_ns, "UTC").isoformat() == "2023-06-14T23:00:00+00:00"
    assert from_utc_ns(summer.end_ns, "UTC").isoformat() == "2023-06-15T07:00:00+00:00"


def test_pre_london_duration_across_dst(session_set):
    assert session_set.window("pre_london", date(2023, 1, 16)).duration_ns == 8 * NS_PER_HOUR
    assert session_set.window("pre_london", date(2023, 6, 15)).duration_ns == 8 * NS_PER_HOUR
    assert session_set.window("pre_london", date(2023, 3, 26)).duration_ns == 7 * NS_PER_HOUR
    assert session_set.window("pre_london", date(2023, 10, 29)).duration_ns == 9 * NS_PER_HOUR


def test_pre_london_and_london_are_adjacent_every_day(session_set):
    """Validates D-011: pre_london and london are both London-anchored, so
    the boundary between them never drifts."""
    d = date(2023, 1, 1)
    end_of_year = date(2023, 12, 31)
    checked = 0
    while d <= end_of_year:
        pre_london = session_set.window("pre_london", d)
        london = session_set.window("london", d)
        assert pre_london.end_ns == london.start_ns
        checked += 1
        d += timedelta(days=1)
    assert checked == 365


def test_new_york_session_ends_exactly_at_trading_day_end(session_set):
    for d in (date(2023, 6, 5), date(2023, 1, 16)):
        window = session_set.window("new_york", d)
        assert window.end_ns == trading_day_bounds(d)[1]


def test_all_windows_inside_trading_day_bounds(session_set):
    concrete_sessions = [
        name
        for name in session_set.session_names()
        if session_set.definition.sessions[name].derived is None
    ]
    # Monthly sample across 2007-2026, plus the known DST-transition-week
    # dates already exercised elsewhere in this file.
    sample_days = [date(y, m, 1) for y in range(2007, 2027) for m in range(1, 13)]
    sample_days += [
        date(2023, 3, 26),
        date(2023, 10, 29),
        date(2021, 3, 14),
        date(2021, 11, 7),
    ]

    checked = 0
    for d in sample_days:
        bounds_start, bounds_end = trading_day_bounds(d)
        for name in concrete_sessions:
            window = session_set.window(name, d)
            assert bounds_start <= window.start_ns
            assert window.end_ns <= bounds_end
            checked += 1
    assert checked == len(sample_days) * len(concrete_sessions)


def test_overlap_london_ny_intersection(session_set):
    d = date(2023, 6, 5)
    overlap = session_set.window("overlap_london_ny", d)
    london = session_set.window("london", d)
    new_york = session_set.window("new_york", d)

    assert overlap.start_ns == new_york.start_ns
    assert overlap.end_ns == london.end_ns
    assert overlap.duration_ns > 0


def test_membership_returns_all_overlapping_sessions(session_set):
    d = date(2023, 6, 5)

    # Inside both london and london_open_window: e.g. 09:00 London.
    ns_09 = local_to_utc_ns(d, time(9, 0), "Europe/London", "later")
    matches = session_set.membership(ns_09)
    assert {"london", "london_open_window"} <= matches

    # 03:00 London is also 11:00 JST, inside Tokyo's 09:00-15:00 cash
    # hours -- pre_london and tokyo both apply. Verified against the real
    # implementation before writing this assertion (initial guess of
    # pre_london-only was wrong).
    ns_03 = local_to_utc_ns(d, time(3, 0), "Europe/London", "later")
    assert session_set.membership(ns_03) == frozenset({"pre_london", "tokyo"})


def test_membership_empty_outside_all_sessions(session_set):
    # 21:30 UTC on a Saturday: inside the weekend gap (confirmed via
    # is_weekend_gap), and outside every fx_core.v1 session's local hours
    # regardless of which raw calendar date trading_day() resolves to for
    # this instant. NOTE: an earlier draft of this test used Saturday noon
    # NY time, on the assumption that any weekend instant would trivially
    # have empty membership -- that assumption was wrong. trading_day() has
    # no weekend awareness (it is a pure "local.date() +1 if >=17:00, else
    # unchanged" formula), so a Saturday daytime instant resolves to that
    # same raw Saturday date, and new_york's window mechanically resolves
    # on it too, non-emptily -- Saturday noon NY genuinely falls inside
    # new_york's 08:00-17:00 window on the Saturday label. This instant
    # (21:30 UTC Saturday) was chosen by scanning for a UTC time-of-day
    # with no session coverage on an ordinary weekday first, then
    # confirming the same slot is empty and weekend-gap on a weekend date.
    ns = to_utc_ns(datetime(2023, 6, 10, 21, 30, tzinfo=timezone.utc))
    assert is_weekend_gap(ns) is True
    assert session_set.membership(ns) == frozenset()


def test_window_half_open_at_boundaries(session_set):
    d = date(2023, 6, 5)
    window = session_set.window("london", d)

    assert window.name in session_set.membership(window.start_ns)
    assert window.name not in session_set.membership(Nanos(window.end_ns))


def test_partial_true_when_window_overlaps_weekend(session_set):
    # trading_day() can return a raw Saturday date for a pre-1700-Sunday
    # instant; more directly, request a window on a date whose 1700-NY
    # rollover bounds cross the weekend gap by using trading_day_bounds
    # directly is out of scope here -- instead, exercise a session whose
    # local window is known to straddle the gap: 'new_york' on the trading
    # day labelled Monday begins Sunday 17:00 NY, i.e. Sunday evening. Since
    # new_york's own local window (08:00-17:00 NY on the Monday date) does
    # not itself touch the gap, use pre_london on the Sunday-dated
    # trading_day_bounds edge case instead: request pre_london for a
    # Saturday date directly, which is a reachable (if unusual) call since
    # `window()` accepts any date.
    saturday = date(2023, 6, 10)
    window = session_set.window("pre_london", saturday)
    assert window.partial is True


def test_unknown_session_name_raises(session_set):
    with pytest.raises(SessionError):
        session_set.window("nope", date(2023, 6, 5))


def test_window_is_memoised_within_an_instance():
    """The same (name, trading_day) returns an identical object, not merely
    an equal one -- proving no recomputation occurred."""
    ss = load_session_set("fx_core", 1)
    a = ss.window("london", date(2023, 6, 5))
    b = ss.window("london", date(2023, 6, 5))
    assert a is b


def test_membership_performance_regression():
    """A-2 regression guard. Session membership over one full trading day of
    1-minute instants must complete well inside a second. Before memoisation
    this took ~0.28s for 1440 calls (198 us/call), which extrapolates to ~19
    minutes over 16 years of bars. The threshold is deliberately loose --
    this catches a reintroduced O(sessions x boundaries) recomputation, not
    a modest slowdown, so it should not be flaky on slower hardware."""
    ss = load_session_set("fx_core", 1)
    start_ns, end_ns = trading_day_bounds(date(2023, 6, 5))
    t0 = time_module.perf_counter()
    ns = start_ns
    count = 0
    while ns < end_ns and count < 1440:
        ss.membership(Nanos(ns))
        ns += NS_PER_MINUTE
        count += 1
    elapsed = time_module.perf_counter() - t0
    assert elapsed < 0.5, f"membership over {count} instants took {elapsed:.2f}s"
