"""Stamp-clock detection and conversion for HistData monthly CSVs.

The fixtures here are hand-built, not sampled from the archive: every
stamp is written out explicitly so a reader can check the arithmetic by
hand. Where a test encodes a fact about the real 2006-2022 archive, the
docstring says so and names the file it came from.
"""

from datetime import datetime, timezone

import numpy as np
import pytest

from jarvis.core.errors import IntegrityError
from jarvis.ingest.histdata import (
    NS_PER_HOUR,
    _naive_ns,
    detect_stamp_clock,
    eu_dst_switch_utc_ns,
    month_has_divergent_dates,
    parse_histdata_csv,
)

_RECORD = "{ts},{bid:.5f},{ask:.5f},0\n"


def _write_csv(path, rows: list[tuple[str, float, float]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for ts, bid, ask in rows:
            f.write(_RECORD.format(ts=ts, bid=bid, ask=ask))


def _stamp(y: int, mo: int, d: int, h: int, mi: int = 0, s: int = 0, ms: int = 0) -> str:
    return f"{y:04d}{mo:02d}{d:02d} {h:02d}{mi:02d}{s:02d}{ms:03d}"


def _utc_hm(ns) -> tuple[int, int]:
    dt = datetime.fromtimestamp(int(ns) / 1e9, tz=timezone.utc)
    return dt.hour, dt.minute


# Which months can discriminate at all ----------------------------------


def test_only_march_october_november_have_divergent_dates():
    """The US and EU daylight-saving calendars agree everywhere except
    two windows a year. Any other month converts identically under either
    stamp clock, which is why detection is neither needed nor possible
    there -- and why nothing is at risk in those months."""
    for year in (2010, 2017, 2019, 2022):
        divergent = [m for m in range(1, 13) if month_has_divergent_dates(year, m)]
        assert divergent == [3, 10, 11], (year, divergent)


# detect_stamp_clock ----------------------------------------------------


def test_detects_us_dst_from_divergent_friday_close():
    """2018-03-16 is inside the spring divergence window (US already on
    EDT, London still on GMT). A 16:59 close means the stamps track New
    York, so the clock is us_dst."""
    stamps = np.array(
        [
            _naive_ns(2018, 3, 16, 16, 0),
            _naive_ns(2018, 3, 16, 16, 59),
            _naive_ns(2018, 3, 18, 17, 1),  # Sunday reopen, after a weekend gap
        ],
        dtype=np.int64,
    )
    clock, evidence = detect_stamp_clock(stamps, 2018, 3)
    assert clock == "us_dst"
    assert "2018-03-16" in evidence and "16:59" in evidence


def test_detects_eu_dst_from_divergent_friday_close():
    """Same divergence window, 2022. A 15:59 close and a 16:00 reopen
    mean the stamps are an hour behind New York -- they are tracking the
    EU calendar, which has not sprung forward yet."""
    stamps = np.array(
        [
            _naive_ns(2022, 3, 18, 15, 0),
            _naive_ns(2022, 3, 18, 15, 59),
            _naive_ns(2022, 3, 20, 16, 1),
        ],
        dtype=np.int64,
    )
    clock, evidence = detect_stamp_clock(stamps, 2022, 3)
    assert clock == "eu_dst"
    assert "15:59" in evidence


def test_non_divergent_month_needs_no_determination():
    """A July file contains no date on which the two calendars disagree,
    so both clocks assign the same UTC to every stamp. Returning None is
    the correct answer, not a failure -- the old code raised here and had
    to be fed a hint to get past it."""
    stamps = np.array(
        [
            _naive_ns(2017, 7, 7, 16, 59),
            _naive_ns(2017, 7, 9, 17, 1),
        ],
        dtype=np.int64,
    )
    clock, evidence = detect_stamp_clock(stamps, 2017, 7)
    assert clock is None
    assert "no date on which the US and EU DST calendars disagree" in evidence


def test_non_divergent_boundary_is_not_counted_as_evidence():
    """A March file's EARLY Fridays are before the US change, so both
    clocks predict the same 16:59 close. Those must not be read as
    us_dst evidence -- doing so is what let an eu_dst March file be
    outvoted by its own non-discriminating days."""
    stamps = np.array(
        [
            _naive_ns(2022, 3, 4, 16, 59),  # pre-US-change: not divergent
            _naive_ns(2022, 3, 6, 17, 1),
            _naive_ns(2022, 3, 18, 15, 59),  # divergent: the only real evidence
            _naive_ns(2022, 3, 20, 16, 1),
        ],
        dtype=np.int64,
    )
    clock, evidence = detect_stamp_clock(stamps, 2022, 3)
    assert clock == "eu_dst"
    assert "2022-03-04" not in evidence


def test_internally_mixed_file_raises():
    stamps = np.array(
        [
            _naive_ns(2022, 3, 18, 16, 59),  # reads us_dst
            _naive_ns(2022, 3, 20, 17, 1),
            _naive_ns(2022, 3, 25, 15, 59),  # reads eu_dst
            _naive_ns(2022, 3, 27, 16, 1),
        ],
        dtype=np.int64,
    )
    with pytest.raises(IntegrityError, match="internally mixed stamp clock"):
        detect_stamp_clock(stamps, 2022, 3)


def test_divergent_month_with_no_readable_boundary_raises():
    """A March file whose clock matters but which offers no readable
    weekend boundary must fail, not guess: guessing shifts part of the
    file by an hour."""
    stamps = np.array(
        [_naive_ns(2022, 3, 15, 12, 0), _naive_ns(2022, 3, 15, 12, 1)],
        dtype=np.int64,
    )
    with pytest.raises(IntegrityError, match="cannot determine the stamp clock"):
        detect_stamp_clock(stamps, 2022, 3)


# The eu_dst switch instants --------------------------------------------


@pytest.mark.parametrize(
    "year,spring,autumn",
    [
        (2019, (2019, 4, 1), (2019, 10, 28)),
        (2020, (2020, 3, 30), (2020, 10, 26)),
        (2021, (2021, 3, 29), (2021, 11, 1)),
        (2022, (2022, 3, 28), (2022, 10, 31)),
    ],
)
def test_eu_switch_is_midnight_utc_on_the_monday_after(year, spring, autumn):
    """Verified against the eight clock discontinuities actually present
    in the 2019-2022 files: each one brackets 00:00 UTC on the Monday
    following the Europe/London change, to within seconds."""
    got_spring, got_autumn = eu_dst_switch_utc_ns(year)
    assert got_spring == _naive_ns(*spring)
    assert got_autumn == _naive_ns(*autumn)


# Conversion ------------------------------------------------------------


def test_us_dst_conversion_summer(tmp_path):
    # May (EDT, UTC-4): 12:00 stamp -> 16:00 UTC.
    csv_path = tmp_path / "DAT_ASCII_GBPUSD_T_201705.csv"
    _write_csv(
        csv_path,
        [
            (_stamp(2017, 5, 15, 12, 0), 1.30000, 1.30010),
            (_stamp(2017, 5, 21, 17, 1), 1.31000, 1.31010),
        ],
    )
    result = parse_histdata_csv(csv_path, "GBPUSD", 2017, 5)
    assert result.stamp_clock is None  # May cannot and need not discriminate
    assert int(result.ts_utc_ns[0]) == _naive_ns(2017, 5, 15, 12, 0) + 4 * NS_PER_HOUR


def test_us_dst_conversion_winter(tmp_path):
    # January (EST, UTC-5): 12:00 stamp -> 17:00 UTC.
    csv_path = tmp_path / "DAT_ASCII_GBPUSD_T_201701.csv"
    _write_csv(
        csv_path,
        [
            (_stamp(2017, 1, 15, 12, 0), 1.30000, 1.30010),
            (_stamp(2017, 1, 21, 17, 1), 1.31000, 1.31010),
        ],
    )
    result = parse_histdata_csv(csv_path, "GBPUSD", 2017, 1)
    assert int(result.ts_utc_ns[0]) == _naive_ns(2017, 1, 15, 12, 0) + 5 * NS_PER_HOUR


def test_eu_dst_divergent_day_converts_an_hour_later_than_us_dst(tmp_path):
    """The whole point of the distinction. 2022-03-15 is inside the
    spring divergence window. Under us_dst a 12:00 stamp would be 16:00
    UTC; under eu_dst it is 17:00 UTC. Getting this wrong moves every
    session boundary in the window by an hour."""
    csv_path = tmp_path / "DAT_ASCII_GBPUSD_T_202203.csv"
    _write_csv(
        csv_path,
        [
            (_stamp(2022, 3, 15, 12, 0), 1.30000, 1.30010),
            (_stamp(2022, 3, 18, 15, 59), 1.31000, 1.31010),  # divergent Friday close
            (_stamp(2022, 3, 20, 16, 1), 1.32000, 1.32010),  # Sunday reopen
        ],
    )
    result = parse_histdata_csv(csv_path, "GBPUSD", 2022, 3)
    assert result.stamp_clock == "eu_dst"
    assert int(result.ts_utc_ns[0]) == _naive_ns(2022, 3, 15, 12, 0) + 5 * NS_PER_HOUR
    assert _utc_hm(result.ts_utc_ns[1]) == (20, 59)  # 16:59 New York, as it must be


def test_eu_dst_spring_switch_is_verified_and_applied(tmp_path):
    """An eu_dst March file changes offset partway through, at 00:00 UTC
    on the Monday after the EU change. Stamps either side of that
    boundary must convert with DIFFERENT offsets -- a single per-file
    offset cannot represent this month at all."""
    csv_path = tmp_path / "DAT_ASCII_GBPUSD_T_202203.csv"
    _write_csv(
        csv_path,
        [
            (_stamp(2022, 3, 18, 15, 59), 1.31000, 1.31010),  # divergent Friday close
            (_stamp(2022, 3, 20, 16, 1), 1.32000, 1.32010),  # Sunday reopen
            (_stamp(2022, 3, 27, 18, 59, 42), 1.33000, 1.33010),  # last before the switch
            (_stamp(2022, 3, 27, 20, 0, 0), 1.33100, 1.33110),  # first after it
            (_stamp(2022, 3, 28, 12, 0), 1.34000, 1.34010),
        ],
    )
    result = parse_histdata_csv(csv_path, "GBPUSD", 2022, 3)
    assert result.stamp_clock == "eu_dst"
    assert "spring clock switch verified" in result.stamp_clock_evidence
    # the one-hour stamp hole is NOT a one-hour hole in real time
    assert int(result.ts_utc_ns[3]) - int(result.ts_utc_ns[2]) == 18 * 1_000_000_000
    # and the post-switch day is on UTC-4, not UTC-5
    assert int(result.ts_utc_ns[4]) == _naive_ns(2022, 3, 28, 12, 0) + 4 * NS_PER_HOUR


def test_eu_dst_autumn_fall_back_repeats_an_hour_and_is_accepted(tmp_path):
    """October 2019-2022 are the only files in the archive whose stamps
    are not ascending: the clock falls back and re-uses an hour it has
    already stamped. The old parser rejected all four outright. They must
    now import, with the repeated hour resolved by POSITION IN THE FILE
    (first pass UTC-4, second pass UTC-5) -- a wall-clock fold policy
    cannot do this."""
    csv_path = tmp_path / "DAT_ASCII_GBPUSD_T_202210.csv"
    _write_csv(
        csv_path,
        [
            (_stamp(2022, 10, 28, 16, 59), 1.31000, 1.31010),  # non-divergent Friday
            (_stamp(2022, 10, 30, 16, 1), 1.32000, 1.32010),  # Sunday reopen
            (_stamp(2022, 10, 30, 19, 30), 1.33000, 1.33010),  # first pass: UTC-4
            (_stamp(2022, 10, 30, 19, 59, 57), 1.33100, 1.33110),  # last before fall-back
            (_stamp(2022, 10, 30, 19, 0, 0), 1.33200, 1.33210),  # clock steps BACK
            (_stamp(2022, 10, 30, 19, 30), 1.33300, 1.33310),  # second pass: UTC-5
            (_stamp(2022, 10, 31, 12, 0), 1.34000, 1.34010),
        ],
    )
    result = parse_histdata_csv(csv_path, "GBPUSD", 2022, 10)
    assert result.stamp_clock == "eu_dst"
    assert "autumn clock switch verified" in result.stamp_clock_evidence
    ts = result.ts_utc_ns
    # the two 19:30 stamps are an hour apart in real time, not identical
    assert int(ts[5]) - int(ts[2]) == NS_PER_HOUR
    # and the output is still ascending in UTC, which is what matters
    assert np.all(np.diff(ts) > 0)
    assert int(ts[6]) == _naive_ns(2022, 10, 31, 12, 0) + 5 * NS_PER_HOUR


def test_backward_step_in_the_wrong_place_is_still_corruption(tmp_path):
    """The fall-back exemption is narrow: exactly one backward step, at
    the instant the rule predicts. A file with the real fall-back PLUS a
    stray out-of-order row is corruption and must still fail."""
    csv_path = tmp_path / "DAT_ASCII_GBPUSD_T_202210.csv"
    _write_csv(
        csv_path,
        [
            (_stamp(2022, 10, 28, 16, 59), 1.31000, 1.31010),
            (_stamp(2022, 10, 30, 16, 1), 1.32000, 1.32010),
            (_stamp(2022, 10, 30, 19, 59, 57), 1.33100, 1.33110),
            (_stamp(2022, 10, 30, 19, 0, 0), 1.33200, 1.33210),  # the real fall-back
            (_stamp(2022, 10, 31, 12, 0), 1.34000, 1.34010),
            (_stamp(2022, 10, 31, 11, 0), 1.34100, 1.34110),  # stray: not the fall-back
        ],
    )
    with pytest.raises(IntegrityError, match="timestamps not ascending"):
        parse_histdata_csv(csv_path, "GBPUSD", 2022, 10)


def test_nfp_anchor_us_dst(tmp_path):
    """NFP releases at 08:30 New York on the first Friday. In an EDT
    month that is 12:30 UTC."""
    csv_path = tmp_path / "DAT_ASCII_GBPUSD_T_201705.csv"
    _write_csv(
        csv_path,
        [
            (_stamp(2017, 5, 5, 8, 30), 1.29500, 1.29510),
            (_stamp(2017, 5, 5, 16, 59), 1.29000, 1.29010),
            (_stamp(2017, 5, 7, 17, 1), 1.31000, 1.31010),
        ],
    )
    result = parse_histdata_csv(csv_path, "GBPUSD", 2017, 5)
    assert _utc_hm(result.ts_utc_ns[0]) == (12, 30)


def test_nfp_anchor_eu_dst_divergent_window(tmp_path):
    """The same anchor inside the divergence window. NFP is still 08:30
    New York, so it is still 12:30 UTC -- but the file stamps it 07:30,
    because the file's clock has not sprung forward. Reading these
    stamps as New York local would put NFP at 11:30 UTC, an hour early."""
    csv_path = tmp_path / "DAT_ASCII_GBPUSD_T_202203.csv"
    _write_csv(
        csv_path,
        [
            (_stamp(2022, 3, 18, 7, 30), 1.29500, 1.29510),  # NFP-style release burst
            (_stamp(2022, 3, 18, 15, 59), 1.29000, 1.29010),  # divergent Friday close
            (_stamp(2022, 3, 20, 16, 1), 1.31000, 1.31010),
        ],
    )
    result = parse_histdata_csv(csv_path, "GBPUSD", 2022, 3)
    assert result.stamp_clock == "eu_dst"
    assert _utc_hm(result.ts_utc_ns[0]) == (12, 30)


def test_pre_2007_dst_rules(tmp_path):
    """November 2004 falls under the pre-2007 US DST regime (DST ended
    the LAST Sunday of October, not the first Sunday of November). A
    November 2, 2004 stamp must convert using EST (-5) -- if
    jarvis.timeengine were projecting modern rules backward, this date
    would be incorrectly treated as still-DST (EDT, -4), since the modern
    rule's November transition had not yet occurred."""
    csv_path = tmp_path / "DAT_ASCII_GBPUSD_T_200411.csv"
    _write_csv(csv_path, [(_stamp(2004, 11, 2, 12, 0), 1.80000, 1.80010)])
    result = parse_histdata_csv(csv_path, "GBPUSD", 2004, 11)
    assert int(result.ts_utc_ns[0]) == _naive_ns(2004, 11, 2, 12, 0) + 5 * NS_PER_HOUR
