from datetime import datetime, timezone

from jarvis.describe.periods import (
    STAGE2_DESCRIPTIVE_END_NS,
    STAGE2_DESCRIPTIVE_START_NS,
    stage2_descriptive_range,
)


def test_range_is_2007_through_2014_inclusive_half_open():
    start_dt = datetime.fromtimestamp(STAGE2_DESCRIPTIVE_START_NS / 1_000_000_000, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(STAGE2_DESCRIPTIVE_END_NS / 1_000_000_000, tz=timezone.utc)
    assert start_dt == datetime(2007, 1, 1, tzinfo=timezone.utc)
    assert end_dt == datetime(2015, 1, 1, tzinfo=timezone.utc)


def test_range_excludes_2015_and_everything_after():
    _start, end = stage2_descriptive_range()
    dec_31_2014 = int(datetime(2014, 12, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp()) * 1_000_000_000
    jan_1_2015 = int(datetime(2015, 1, 1, tzinfo=timezone.utc).timestamp()) * 1_000_000_000
    assert dec_31_2014 < end
    assert jan_1_2015 == end  # half-open: end itself is excluded


def test_returns_the_module_level_constants():
    start, end = stage2_descriptive_range()
    assert start == STAGE2_DESCRIPTIVE_START_NS
    assert end == STAGE2_DESCRIPTIVE_END_NS
