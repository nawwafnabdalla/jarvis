from datetime import date
from pathlib import Path

import polars as pl
import pytest

from jarvis.core.errors import ConfigError
from jarvis.events import ScheduledReleaseCalendar, load_scheduled_releases
from jarvis.events.calendar import _validate_schema


def test_real_calendar_loads_and_validates():
    cal = load_scheduled_releases()
    assert cal.calendar_id == "scheduled_releases"
    assert cal.version == 1
    assert len(cal.by_date) > 0


def test_known_nfp_friday_is_flagged():
    # 2010-01-08 is the first Friday of January 2010 -- a real, ordinary
    # (non-shutdown-affected) NFP release day.
    cal = load_scheduled_releases()
    assert cal.is_scheduled_release_day(date(2010, 1, 8))
    assert "nfp" in cal.event_types_on(date(2010, 1, 8))


def test_known_fomc_date_is_flagged():
    # 2012-01-25, the second day of the January 2012 FOMC meeting,
    # verified directly against federalreserve.gov's own historical page.
    cal = load_scheduled_releases()
    assert cal.is_scheduled_release_day(date(2012, 1, 25))
    assert "fomc" in cal.event_types_on(date(2012, 1, 25))


def test_known_boe_date_is_flagged():
    # 2013-07-04, the confirmed (not the earlier provisional) July 2013
    # BoE MPC decision date.
    cal = load_scheduled_releases()
    assert cal.is_scheduled_release_day(date(2013, 7, 4))
    assert "boe_mpc" in cal.event_types_on(date(2013, 7, 4))


def test_documented_shutdown_exception_reflects_actual_release_date():
    # The September-2013-data NFP release was delayed to 2013-10-22 by
    # the government shutdown -- the standard first-Friday date
    # (2013-10-04) must NOT be flagged as nfp for this reason, and the
    # actual delayed date must be.
    cal = load_scheduled_releases()
    assert "nfp" in cal.event_types_on(date(2013, 10, 22))
    assert cal.event_types_on(date(2013, 10, 4)) == ()


def test_independence_day_holiday_shift_reflects_actual_release_date():
    # The naive "first Friday of the month" rule would put June 2008's
    # and June 2014's NFP release on July 4 (Independence Day, a federal
    # holiday) -- found by systematically checking every month where the
    # rule collides with a fixed US holiday, not assumed. The actual
    # historical releases were shifted one day earlier, to Thursday.
    cal = load_scheduled_releases()
    assert "nfp" in cal.event_types_on(date(2008, 7, 3))
    assert cal.event_types_on(date(2008, 7, 4)) == ()
    assert "nfp" in cal.event_types_on(date(2014, 7, 3))
    assert cal.event_types_on(date(2014, 7, 4)) == ()


def test_new_year_holiday_shift_reflects_actual_release_date():
    # 2010-01-01 (New Year's Day) is the naive "first Friday" of January
    # 2010; the actual December-2009-data release was 2010-01-08 (the
    # second Friday), not the first.
    cal = load_scheduled_releases()
    assert "nfp" in cal.event_types_on(date(2010, 1, 8))
    assert cal.event_types_on(date(2010, 1, 1)) == ()


def test_ordinary_day_is_not_flagged():
    # A random Tuesday with no known scheduled release.
    cal = load_scheduled_releases()
    assert not cal.is_scheduled_release_day(date(2010, 1, 12))
    assert cal.event_types_on(date(2010, 1, 12)) == ()


def test_no_duplicate_event_types_on_a_single_day():
    cal = load_scheduled_releases()
    for d, types in cal.by_date.items():
        assert len(types) == len(set(types)), f"duplicate event_type on {d}: {types}"


def test_date_range_is_within_2007_2014():
    cal = load_scheduled_releases()
    assert min(cal.by_date) >= date(2007, 1, 1)
    assert max(cal.by_date) <= date(2014, 12, 31)


def test_missing_calendar_version_raises_config_error():
    with pytest.raises(ConfigError, match="not found"):
        load_scheduled_releases(version=999)


# ---------------------------------------------------------------------------
# Schema validation, directly against synthetic frames.
# ---------------------------------------------------------------------------


def test_schema_rejects_missing_column():
    frame = pl.DataFrame({"date": [date(2010, 1, 1)], "event_type": ["nfp"]})
    with pytest.raises(ConfigError, match="missing required column"):
        _validate_schema(frame, path=Path("x.csv"))


def test_schema_rejects_unknown_event_type():
    frame = pl.DataFrame(
        {"date": [date(2010, 1, 1)], "event_type": ["not_a_real_type"], "source_note": ["x"]}
    )
    with pytest.raises(ConfigError, match="unknown event_type"):
        _validate_schema(frame, path=Path("x.csv"))


def test_schema_rejects_null_date():
    frame = pl.DataFrame(
        {"date": pl.Series([None], dtype=pl.Date), "event_type": ["nfp"], "source_note": ["x"]}
    )
    with pytest.raises(ConfigError, match="date"):
        _validate_schema(frame, path=Path("x.csv"))


def test_schema_accepts_a_valid_frame():
    frame = pl.DataFrame(
        {"date": [date(2010, 1, 1)], "event_type": ["fomc"], "source_note": ["x"]}
    )
    _validate_schema(frame, path=Path("x.csv"))  # must not raise
