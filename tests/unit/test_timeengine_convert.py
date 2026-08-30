import random
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from jarvis.core.errors import AmbiguousTimeError, UserError
from jarvis.core.types import Nanos
from jarvis.timeengine.convert import (
    NS_PER_SECOND,
    from_utc_ns,
    is_ambiguous,
    is_nonexistent,
    local_to_utc_ns,
    to_utc_ns,
)

_LONDON = "Europe/London"
_NY = "America/New_York"
_TOKYO = "Asia/Tokyo"


def _london_08_to_ny(d: date) -> str:
    dt_london = datetime(d.year, d.month, d.day, 8, 0, tzinfo=ZoneInfo(_LONDON))
    ns = to_utc_ns(dt_london)
    ny_dt = from_utc_ns(ns, _NY)
    return ny_dt.strftime("%H:%M")


# T1 -----------------------------------------------------------------------


def test_spring_divergence_window_london_to_ny():
    assert _london_08_to_ny(date(2023, 3, 10)) == "03:00"
    assert _london_08_to_ny(date(2023, 3, 13)) == "04:00"  # corrected, per WP-003 notice
    assert _london_08_to_ny(date(2023, 3, 24)) == "04:00"  # corrected, per WP-003 notice
    assert _london_08_to_ny(date(2023, 3, 27)) == "03:00"


# T2 -----------------------------------------------------------------------


def test_autumn_divergence_window_london_to_ny():
    assert _london_08_to_ny(date(2023, 10, 27)) == "03:00"
    assert _london_08_to_ny(date(2023, 10, 30)) == "04:00"
    assert _london_08_to_ny(date(2023, 11, 3)) == "04:00"
    assert _london_08_to_ny(date(2023, 11, 6)) == "03:00"


# T3 -----------------------------------------------------------------------


def test_nonexistent_ny_spring_forward_later():
    d, t = date(2021, 3, 14), time(2, 30)

    assert is_nonexistent(d, t, _NY) is True

    ns = local_to_utc_ns(d, t, _NY, "later")
    resolved = from_utc_ns(ns, _NY)
    assert (resolved.date(), resolved.time()) == (date(2021, 3, 14), time(3, 0))
    assert resolved.tzname() == "EDT"

    with pytest.raises(AmbiguousTimeError):
        local_to_utc_ns(d, t, _NY)  # default policy: raise


# T4 -----------------------------------------------------------------------


def test_ambiguous_ny_fall_back_policies():
    d, t = date(2021, 11, 7), time(1, 30)

    assert is_ambiguous(d, t, _NY) is True

    earlier_ns = local_to_utc_ns(d, t, _NY, "earlier")
    later_ns = local_to_utc_ns(d, t, _NY, "later")

    assert later_ns - earlier_ns == NS_PER_SECOND * 3600
    assert earlier_ns < later_ns

    with pytest.raises(AmbiguousTimeError):
        local_to_utc_ns(d, t, _NY)


# T5 -----------------------------------------------------------------------


def test_nonexistent_london_spring_forward():
    d, t = date(2023, 3, 26), time(1, 30)

    assert is_nonexistent(d, t, _LONDON) is True

    ns = local_to_utc_ns(d, t, _LONDON, "later")
    resolved = from_utc_ns(ns, _LONDON)
    assert (resolved.date(), resolved.time()) == (date(2023, 3, 26), time(2, 0))
    assert resolved.tzname() == "BST"


# T7 -----------------------------------------------------------------------


def test_tokyo_has_no_dst():
    for d in (date(2023, 1, 15), date(2023, 7, 15), date(2007, 1, 15), date(2007, 7, 15)):
        dt = from_utc_ns(local_to_utc_ns(d, time(12, 0), _TOKYO), _TOKYO)
        assert dt.utcoffset().total_seconds() == 9 * 3600

    # No date in 2007-2026 is ambiguous or non-existent in Tokyo.
    rng = random.Random(7)
    epoch_start = to_utc_ns(datetime(2007, 1, 1, tzinfo=timezone.utc))
    epoch_end = to_utc_ns(datetime(2026, 12, 31, tzinfo=timezone.utc))
    for _ in range(2000):
        ns = Nanos(rng.randrange(epoch_start, epoch_end))
        d, t = from_utc_ns(ns, _TOKYO).date(), from_utc_ns(ns, _TOKYO).time()
        assert is_ambiguous(d, t, _TOKYO) is False
        assert is_nonexistent(d, t, _TOKYO) is False


# T8 -----------------------------------------------------------------------


def test_historical_us_dst_rules_not_projected_backward():
    """The critical test. Pre-2007 US DST began the first Sunday in April;
    from 2007 the second Sunday in March. A library projecting today's
    rules backward would report DST active on 2006-03-12 and this test
    would fail."""
    ny = ZoneInfo(_NY)

    dt_2006_03_12 = datetime(2006, 3, 12, 12, 0, tzinfo=ny)
    dt_2006_04_02 = datetime(2006, 4, 2, 12, 0, tzinfo=ny)
    dt_2007_03_11 = datetime(2007, 3, 11, 12, 0, tzinfo=ny)

    assert dt_2006_03_12.dst().total_seconds() == 0  # old rule: DST not yet started
    assert dt_2006_04_02.dst().total_seconds() == 3600  # old rule: first Sunday in April
    assert dt_2007_03_11.dst().total_seconds() == 3600  # new rule: second Sunday in March


# T12 ------------------------------------------------------------------


def test_pre_london_window_utc_boundaries():
    winter_start = local_to_utc_ns(date(2023, 1, 15), time(0, 0), _LONDON)
    winter_end = local_to_utc_ns(date(2023, 1, 15), time(8, 0), _LONDON)
    assert from_utc_ns(winter_start, "UTC").isoformat() == "2023-01-15T00:00:00+00:00"
    assert from_utc_ns(winter_end, "UTC").isoformat() == "2023-01-15T08:00:00+00:00"

    summer_start = local_to_utc_ns(date(2023, 6, 15), time(0, 0), _LONDON)
    summer_end = local_to_utc_ns(date(2023, 6, 15), time(8, 0), _LONDON)
    assert from_utc_ns(summer_start, "UTC").isoformat() == "2023-06-14T23:00:00+00:00"
    assert from_utc_ns(summer_end, "UTC").isoformat() == "2023-06-15T07:00:00+00:00"


# T13 ------------------------------------------------------------------


def _roundtrip_check(iterations: int, seed: int) -> None:
    rng = random.Random(seed)
    start_ns = to_utc_ns(datetime(2007, 1, 1, tzinfo=timezone.utc))
    end_ns = to_utc_ns(datetime(2026, 12, 31, tzinfo=timezone.utc))
    zones = (_LONDON, _NY, _TOKYO)

    for _ in range(iterations):
        # whole-microsecond ns values only, per the spec's stated precision
        us = rng.randrange(start_ns // 1000, end_ns // 1000)
        ns = Nanos(us * 1000)
        tz = rng.choice(zones)
        assert to_utc_ns(from_utc_ns(ns, tz)) == ns


_ROUNDTRIP_START_US = to_utc_ns(datetime(2007, 1, 1, tzinfo=timezone.utc)) // 1000
_ROUNDTRIP_END_US = to_utc_ns(datetime(2026, 12, 31, tzinfo=timezone.utc)) // 1000


@given(
    us=st.integers(min_value=_ROUNDTRIP_START_US, max_value=_ROUNDTRIP_END_US),
    tz=st.sampled_from((_LONDON, _NY, _TOKYO)),
)
def test_roundtrip_property(us: int, tz: str) -> None:
    """Property test: for instants across 2007-2026 in all three zones,
    to_utc_ns(from_utc_ns(ns, tz)) == ns for whole-microsecond ns values.
    Closes carried debt D-030a (WP-005 item 0)."""
    ns = Nanos(us * 1000)
    assert to_utc_ns(from_utc_ns(ns, tz)) == ns


@pytest.mark.slow
def test_roundtrip_property_slow():
    _roundtrip_check(1_000_000, seed=13)


# Unnamed required tests ------------------------------------------------


def test_naive_datetime_rejected():
    with pytest.raises(UserError):
        to_utc_ns(datetime(2023, 1, 1, 12))


def test_unknown_timezone_rejected():
    with pytest.raises(UserError):
        from_utc_ns(Nanos(0), "Mars/Olympus_Mons")


def test_sub_microsecond_truncates_toward_zero():
    dt = from_utc_ns(Nanos(1_000_000_000 + 1_500), "UTC")
    assert dt.microsecond == 1
