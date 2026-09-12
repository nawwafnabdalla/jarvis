"""HistData.com CSV parsing and per-file stamp-clock detection.

HistData's own FAQ claims timestamps are "EST without daylight saving
adjustments." That is wrong for the whole 2006-2022 range: the stamps are
DST-aware. But they are not on one clock throughout, and the earlier
reading of the anomaly -- "most months are America/New_York, some March
and November months are a fixed UTC-5" -- was also wrong. A fixed UTC-5
clock does not exist anywhere in this archive.

THE FINDING (WP-009-TZ, full 196-month archive, 2006-09 .. 2022-12).
There are two eras, and they differ in WHICH DAYLIGHT-SAVING CALENDAR the
stamps follow, not in whether they observe DST at all:

  us_dst  (2006-09 .. 2018-12)  stamps are America/New_York wall clock:
      UTC-5 in US winter, UTC-4 in US summer, changing on the US dates
      (2nd Sunday March / 1st Sunday November).
  eu_dst  (2019-01 .. 2022-12)  stamps are UTC-5 in the EU winter period
      and UTC-4 in the EU summer period, changing on the EU dates
      (last Sunday March / last Sunday October) -- specifically at
      00:00 UTC on the MONDAY FOLLOWING each EU change.

The two calendars agree for about eleven months of the year, which is why
this went unnoticed: they differ ONLY inside two windows -- US-change to
EU-change in spring, and EU-change to US-change in autumn. Inside those
windows New York is UTC-4 while London is UTC+0, so the two clocks put
every stamp an hour apart. Everywhere else the two eras are
byte-for-byte indistinguishable AND interchangeable: a file containing no
divergent date converts identically under either clock, so for those
files there is nothing to determine and nothing at risk.

EVIDENCE (all of it discriminating, spanning the full range, per D-054):

  1. Weekend boundaries on divergent weeks -- the hardest anchor in the
     file, since the market's Friday 17:00 NY close and Sunday 17:00 NY
     reopen are structural, not statistical. Across the whole archive:
     70/70 divergent-week boundaries in 2007-2018 read us_dst; 24/24 in
     2019-2022 read eu_dst. No exceptions, no ambiguity.
  2. The stamp clock's own discontinuities. A DST-aware clock leaves a
     mark when it changes: a one-hour hole when it springs forward, and
     a backward step (a repeated hour) when it falls back. Scanning every
     consecutive stamp pair of all 196 files finds exactly eight, all in
     2019-2022, all on EU change dates, all matching the 00:00-UTC-Monday
     rule to within seconds. 2006-2018 contains none -- its US-calendar
     changes fall at 02:00 NY on a Sunday, inside the weekend closure,
     where they are invisible.

CONSEQUENCE FOR THIS MODULE. The stamp clock is not a single offset per
file: an eu_dst March or October file changes offset partway through, and
an eu_dst October file is NOT monotonic in stamp order -- it revisits an
hour it has already used. Both are handled explicitly below, and both are
VERIFIED against where the rule says they must be rather than assumed. A
file whose discontinuity is missing, doubled, or in the wrong place fails
loudly rather than silently shifting an hour of ticks.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Literal

import numpy as np
import polars as pl

from jarvis.core.errors import IntegrityError
from jarvis.core.hashing import sha256_file
from jarvis.core.types import Nanos
from jarvis.timeengine import is_ambiguous, is_nonexistent, local_to_utc_ns

NS_PER_HOUR = 3_600_000_000_000
NS_PER_MINUTE = 60_000_000_000
NS_PER_SECOND = 1_000_000_000
_NS_PER_DAY = 24 * NS_PER_HOUR
# WP-009-CORRECTION: a > 1 hour threshold fires on ordinary intraday data
# holes (HistData's own .txt reports routinely show 60-150s gaps, and
# occasionally much larger ones from feed outages), not just genuine
# weekend closures -- confirmed against real 2023-05 data, which has 135
# intraday gaps over an hour but only 4 real weekend closes. A real
# weekend is ~48h (Fri 17:00 NY to Sun 17:00 NY); 40h has comfortable
# margin below that while sitting far above any plausible intraday hole.
WEEKEND_GAP_MIN_NS = 40 * NS_PER_HOUR
_FRIDAY = 4  # Python's date.weekday(): Monday=0 .. Sunday=6
_SUNDAY = 6
_MAX_PLAUSIBLE_WEEKEND_GAPS = 6  # a normal month has 4 or 5; see detect_stamp_clock
# A us_dst file's DST changes fall at 02:00 New York on a Sunday, inside
# the weekend closure, so essentially no row should need per-row
# ambiguous/non-existent resolution. More than a handful means something
# is wrong with the file, not with the conversion.
_AMBIGUOUS_CORRECTION_WARN_THRESHOLD = 10

# A clock discontinuity is one hour wide. The observed eight land within
# ~32s of the exact boundary (the market simply has no tick in the last
# few seconds before it), so the tolerance below is about tick sparsity,
# not about any uncertainty in where the boundary is.
_SWITCH_TOLERANCE_NS = 5 * NS_PER_MINUTE

# The two offsets an eu_dst clock takes, as UTC offsets (negative west of
# Greenwich), matching _zone_offset_ns' sign convention.
_UTC_MINUS_5 = -5 * NS_PER_HOUR
_UTC_MINUS_4 = -4 * NS_PER_HOUR

StampClock = Literal["us_dst", "eu_dst"]
ColumnOrder = Literal["bid_ask", "ask_bid"]

_TS_RE = re.compile(r"^\d{8} \d{9}$")

# WP-009-CORRECTION finding 2: real HistData exports for 2006-09 through
# 2009-04 have their price columns as (ask, bid), not the documented
# (bid, ask) -- confirmed 100% inverted (col2 > col3) across every row of
# every one of those months, and the documented order from 2009-06
# onward. 2009-05 is the changeover month and is NOT clean: it switches
# mid-month, at a weekend boundary -- May 1-22 are 100% (ask, bid) and
# May 24-31 are 0%, so the file-level fraction is 0.7618 and the check
# below correctly refuses it. See docs/WP-009-TZ-FINDING.md section 5.2;
# reading column order per day would import it, and that is an open
# decision, not something to guess. A spread is positive by
# definition (ask > bid) on essentially every tick, so the fraction of
# rows with col2 > col3 is decisive; 1% tolerance absorbs genuine
# zero-spread thin quotes (observed: 5 rows out of 554,877 in a clean
# 2009-11 file) without letting a genuinely mixed file through.
_COLUMN_ORDER_HIGH_THRESHOLD = 0.99
_COLUMN_ORDER_LOW_THRESHOLD = 0.01


@dataclass(frozen=True, slots=True)
class HistDataMonth:
    instrument: str
    year: int
    month: int
    # None when the file has no ticks on a date where the US and EU DST
    # calendars disagree -- both clocks then give the same UTC for every
    # stamp in it, so no determination was needed.
    stamp_clock: StampClock | None
    stamp_clock_evidence: str  # human-readable, goes in the import report
    column_order: ColumnOrder
    column_order_evidence: str  # human-readable, goes in the import report
    ts_utc_ns: np.ndarray  # int64, ascending
    bid: np.ndarray  # float64
    ask: np.ndarray  # float64
    row_count: int
    source_path: Path
    source_sha256: str


def detect_column_order(col2: np.ndarray, col3: np.ndarray) -> tuple[ColumnOrder, str]:
    """Determine whether a file's second and third CSV columns are
    (bid, ask) -- the documented order -- or (ask, bid).

    A spread is positive by definition: ask > bid on essentially every
    tick. The fraction of rows with col2 > col3 is therefore decisive:
      <= 1%  -> "bid_ask"  (col2 is bid, the documented order)
      >= 99% -> "ask_bid"  (col2 is ask)
      otherwise -> IntegrityError naming the exact fraction: the file is
        internally inconsistent between the two orders and cannot be
        imported safely under either interpretation -- this needs a
        human decision, not a guess.

    Returns the order plus a one-line evidence string giving the
    fraction and row count."""
    n = len(col2)
    if n == 0:
        raise IntegrityError("detect_column_order: no rows to evaluate")

    frac_col2_gt_col3 = float(np.count_nonzero(col2 > col3)) / n

    if frac_col2_gt_col3 <= _COLUMN_ORDER_LOW_THRESHOLD:
        return (
            "bid_ask",
            f"{frac_col2_gt_col3:.4f} of {n} rows have col2 > col3 -- "
            "documented (bid, ask) column order",
        )
    if frac_col2_gt_col3 >= _COLUMN_ORDER_HIGH_THRESHOLD:
        return (
            "ask_bid",
            f"{frac_col2_gt_col3:.4f} of {n} rows have col2 > col3 -- "
            "column order is (ask, bid), not the documented (bid, ask)",
        )

    raise IntegrityError(
        f"internally inconsistent column order: {frac_col2_gt_col3:.4f} of {n} rows "
        "have col2 > col3 (neither >= 0.99 nor <= 0.01) -- this file cannot be "
        "imported safely under either (bid, ask) or (ask, bid); requires a human "
        "decision, not an automatic guess"
    )


def _naive_ns(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0, ms: int = 0) -> int:
    """Nanoseconds since the Unix epoch for (y,mo,d,h,mi,s,ms) treated AS
    IF it were UTC -- a naive-arithmetic device, not a real UTC instant.
    Used only for sortable/diffable representation of a local wall-clock
    stamp whose real zone is not yet known (or is being determined)."""
    return (
        int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp()) * NS_PER_SECOND
        + ms * 1_000_000
    )


def _zone_offset_ns(d: date, tz: str, probe: time = time(12, 0)) -> int:
    """The zone's UTC offset on this date, in nanoseconds, as
    naive_ns(d, probe) - real_utc_ns(d, probe) -- NEGATIVE west of
    Greenwich (America/New_York in winter returns -5h). A naive-as-utc
    local stamp converts to the real UTC instant by SUBTRACTING this
    value, which is what _convert_ny_local and _convert_eu_dst both do.
    Probed at noon, which is never itself ambiguous or non-existent
    (both US and EU DST transitions occur in the small hours)."""
    naive = _naive_ns(d.year, d.month, d.day, probe.hour, probe.minute)
    return naive - int(local_to_utc_ns(d, probe, tz, "later"))


def _ny_offset_ns(d: date, probe: time = time(12, 0)) -> int:
    return _zone_offset_ns(d, "America/New_York", probe)


def _eu_clock_offset_ns(d: date) -> int:
    """The eu_dst clock's UTC offset on date `d`: -5h in the EU winter
    period, -4h in the EU summer period. Derived from Europe/London's
    real offset, never from hardcoded change dates."""
    return _zone_offset_ns(d, "Europe/London") - 5 * NS_PER_HOUR


def _is_divergent(d: date) -> bool:
    """True when the US and EU daylight-saving calendars disagree about
    this date -- the only dates on which the two stamp clocks differ, and
    therefore the only dates that carry any evidence about which clock a
    file is on."""
    return _ny_offset_ns(d) != _eu_clock_offset_ns(d)


def _eu_change_sunday(year: int, month: int) -> date:
    """The date on which Europe/London's own offset changes in `month` of
    `year`, found by asking jarvis.timeengine what the offset actually is
    on each day -- never by hand-rolled "last Sunday of the month"
    arithmetic."""
    first = date(year, month, 1)
    start_offset = _zone_offset_ns(first, "Europe/London")
    d = first
    while d.month == month:
        if _zone_offset_ns(d, "Europe/London") != start_offset:
            return d
        d = d + timedelta(days=1)
    raise IntegrityError(
        f"no Europe/London DST change found in {year:04d}-{month:02d}; "
        "the installed tzdata disagrees with the EU DST calendar this code assumes"
    )


def eu_dst_switch_utc_ns(year: int) -> tuple[int, int]:
    """(spring, autumn) UTC instants at which an eu_dst-stamped file
    changes offset: 00:00 UTC on the MONDAY FOLLOWING each Europe/London
    change. This is NOT the EU transition instant itself (01:00 UTC on
    the Sunday) -- it is roughly 23 hours later, and the difference is
    directly observable: it is why the clock discontinuity shows up
    mid-session on Sunday evening rather than inside the weekend closure.
    Verified to within seconds against all eight discontinuities present
    in the 2019-2022 files."""
    out = []
    for month in (3, 10):
        monday = _eu_change_sunday(year, month) + timedelta(days=1)
        out.append(_naive_ns(monday.year, monday.month, monday.day))
    return out[0], out[1]


def month_has_divergent_dates(year: int, month: int) -> bool:
    """True when this calendar month contains at least one date on which
    the US and EU daylight-saving calendars disagree. Only March, October
    and November ever do."""
    d = date(year, month, 1)
    while d.month == month:
        if _is_divergent(d):
            return True
        d = d + timedelta(days=1)
    return False


def _stamp_dt(ns: int) -> datetime:
    return datetime.fromtimestamp(ns // NS_PER_SECOND, tz=timezone.utc)


# Ticks strictly inside the hour an eu_dst clock would have skipped. A
# handful could be stragglers around the boundary; a real session's worth
# cannot, and means the clock never skipped anything.
_MIN_TICKS_INSIDE_SKIPPED_HOUR = 10


def _collect_switch_evidence(
    local_stamps: np.ndarray,
    year: int,
    month: int,
    us_evidence: list[str],
    eu_evidence: list[str],
) -> None:
    """Read the stamp clock's own discontinuities, where the file spans
    one. See detect_stamp_clock for why this is load-bearing for October."""
    if len(local_stamps) < 2:
        return
    lo, hi = int(local_stamps[0]), int(local_stamps[-1])

    for kind, _boundary, stamp_before, stamp_after in _eu_switch_stamps(year, month):
        skipped_lo, skipped_hi = min(stamp_before, stamp_after), max(stamp_before, stamp_after)
        if not (lo < skipped_lo and hi > skipped_hi):
            continue  # the file does not span this instant: it says nothing

        if kind == "spring":
            inside = int(
                np.count_nonzero(
                    (local_stamps > skipped_lo + _SWITCH_TOLERANCE_NS)
                    & (local_stamps < skipped_hi - _SWITCH_TOLERANCE_NS)
                )
            )
            when = f"{_stamp_dt(skipped_lo):%Y-%m-%d %H:%M}-{_stamp_dt(skipped_hi):%H:%M}"
            if inside >= _MIN_TICKS_INSIDE_SKIPPED_HOUR:
                us_evidence.append(
                    f"{inside} ticks inside {when}, the hour an eu_dst clock skips (us_dst)"
                )
            else:
                eu_evidence.append(
                    f"one-hour stamp hole at {when}, the eu_dst spring switch (eu_dst)"
                )
        else:
            back = np.nonzero(np.diff(local_stamps) < 0)[0]
            if len(back) == 1:
                i = int(back[0])
                if (
                    abs(int(local_stamps[i]) - stamp_before) <= _SWITCH_TOLERANCE_NS
                    and abs(int(local_stamps[i + 1]) - stamp_after) <= _SWITCH_TOLERANCE_NS
                ):
                    eu_evidence.append(
                        f"stamps step back {_stamp_dt(int(local_stamps[i])):%Y-%m-%d %H:%M:%S}"
                        f" -> {_stamp_dt(int(local_stamps[i + 1])):%H:%M:%S} at the eu_dst "
                        "autumn switch (eu_dst)"
                    )
            elif len(back) == 0:
                us_evidence.append(
                    f"stamps run straight through "
                    f"{_stamp_dt(skipped_lo):%Y-%m-%d %H:%M}-{_stamp_dt(skipped_hi):%H:%M} "
                    "without the backward step an eu_dst clock makes there (us_dst)"
                )


def detect_stamp_clock(
    local_stamps: np.ndarray,
    year: int,
    month: int,
) -> tuple[StampClock | None, str]:
    """Determine, from a file's own content, which daylight-saving
    calendar its stamps follow.

    `local_stamps` is the naive-as-UTC nanosecond representation (see
    _naive_ns) of every tick's wall-clock stamp, in file order.

    Returns (clock, evidence). `clock` is None -- and that is a normal,
    safe outcome, not a failure -- when the file has no ticks on any date
    where the two calendars disagree. Both clocks then assign the SAME UTC
    instant to every stamp in it, so there is nothing to determine and
    nothing a wrong answer could damage. Only March, October and November
    files ever need a determination, and not always even those.

    METHOD. Weekend boundaries are the hardest anchor available: the
    market closes 17:00 New York on Friday and reopens 17:00 New York on
    Sunday, and both edges are structural rather than statistical. Each
    gap >= WEEKEND_GAP_MIN_NS (40 hours -- NOT merely "> 1 hour", since
    ordinary intraday data holes routinely exceed an hour) contributes its
    closing and reopening stamp. On a divergent date:

        close 16:xx / reopen 17:xx  ->  us_dst
        close 15:xx / reopen 16:xx  ->  eu_dst

    Boundaries on NON-divergent dates are not evidence (both clocks
    predict the same stamp) and are ignored rather than counted.

    EXCLUDED: Sunday boundaries on a Europe/London change date. The
    eu_dst clock changes at 00:00 UTC on the Monday AFTER the EU change,
    so that Sunday's session straddles the switch; empirically the week
    also opens an hour away from its usual 17:00 New York on those four
    Sundays, in opposite directions in spring and autumn. Whatever
    produces that, such a boundary cannot be read cleanly under either
    clock, so it is dropped rather than guessed at. This costs nothing:
    every affected window still carries an unambiguous Friday close.

    SECOND, INDEPENDENT SOURCE: the clock's own discontinuity. March and
    October files are the ones that contain an eu_dst switch, and an
    eu_dst switch is directly visible -- a one-hour hole in the stamps in
    spring, a one-hour backward step in autumn. Its presence proves
    eu_dst; its absence, in a file that spans the instant, proves us_dst
    (a us_dst clock runs straight through, so those stamps are simply
    there and in order). This matters because October files have NO
    usable weekend boundary at all: the only divergent weekend day they
    contain is the Europe/London change Sunday, which is excluded above.
    Without the discontinuity, the four October files that actually
    change clock could not be read.

    Raises IntegrityError if the file is internally mixed (some divergent
    boundaries reading us_dst, others eu_dst), or if it shows more
    weekend gaps than a month can plausibly contain."""
    us_evidence: list[str] = []
    eu_evidence: list[str] = []
    ignored: list[str] = []
    weekend_gap_count = 0

    _collect_switch_evidence(local_stamps, year, month, us_evidence, eu_evidence)

    if len(local_stamps) >= 2:
        gaps = np.diff(local_stamps)
        for idx in np.nonzero(gaps >= WEEKEND_GAP_MIN_NS)[0]:
            weekend_gap_count += 1
            for ns, weekday, kind, us_hour, eu_hour in (
                (int(local_stamps[idx]), _FRIDAY, "close", 16, 15),
                (int(local_stamps[idx + 1]), _SUNDAY, "reopen", 17, 16),
            ):
                dt_ = _stamp_dt(ns)
                d = dt_.date()
                if d.weekday() != weekday:
                    continue
                label = f"{d.isoformat()} {kind}={dt_.hour:02d}:{dt_.minute:02d}"
                if not _is_divergent(d):
                    continue  # both clocks agree here: carries no information
                if weekday == _SUNDAY and _zone_offset_ns(
                    d, "Europe/London"
                ) != _zone_offset_ns(d - timedelta(days=1), "Europe/London"):
                    ignored.append(f"{label} (Europe/London change date)")
                    continue
                if dt_.hour == us_hour:
                    us_evidence.append(f"{label} (us_dst)")
                elif dt_.hour == eu_hour:
                    eu_evidence.append(f"{label} (eu_dst)")
                else:
                    # Not a recognisable weekend boundary under either
                    # clock -- not evidence, and not a contradiction.
                    ignored.append(f"{label} (unrecognised)")

    if weekend_gap_count > _MAX_PLAUSIBLE_WEEKEND_GAPS:
        raise IntegrityError(
            f"{year:04d}-{month:02d}: found {weekend_gap_count} weekend gaps "
            f"(>= {WEEKEND_GAP_MIN_NS // NS_PER_HOUR}h) -- a normal month has 4 or 5; "
            f"more than {_MAX_PLAUSIBLE_WEEKEND_GAPS} means this file has structural "
            "problems and its evidence is not trustworthy"
        )

    if us_evidence and eu_evidence:
        raise IntegrityError(
            f"{year:04d}-{month:02d}: internally mixed stamp clock -- divergent-week "
            f"boundaries reading us_dst: {'; '.join(us_evidence)}; reading eu_dst: "
            f"{'; '.join(eu_evidence)}. A file must be on one clock throughout; this "
            "one cannot be imported safely under either."
        )

    suffix = f" (ignored: {'; '.join(ignored)})" if ignored else ""
    if us_evidence:
        return "us_dst", (
            f"{len(us_evidence)} divergent-week boundary(ies): " + "; ".join(us_evidence) + suffix
        )
    if eu_evidence:
        return "eu_dst", (
            f"{len(eu_evidence)} divergent-week boundary(ies): " + "; ".join(eu_evidence) + suffix
        )

    # The question is not whether the CALENDAR month contains a divergent
    # date, but whether this FILE has any ticks on one. November 2008 and
    # November 2014 are the case in point: each has exactly one divergent
    # date, and it falls on a Saturday, when the market is shut. Neither
    # file contains a single tick the two clocks would convert
    # differently, so neither needs a determination -- keying off the
    # calendar alone would hard-fail both for no reason.
    # Reduce to unique days with integer arithmetic before touching the
    # calendar: local_stamps is naive-as-UTC, so floor-dividing by a day
    # gives the day index directly. That is at most ~31 _is_divergent
    # calls per month instead of one per tick -- and this branch is the
    # COMMON path (every month that needs no determination reaches it),
    # so a per-tick loop here would cost far more than the detection does.
    day_index = np.unique(local_stamps // _NS_PER_DAY)
    divergent_dates = sorted(
        d for d in (_stamp_dt(int(k) * _NS_PER_DAY).date() for k in day_index) if _is_divergent(d)
    )
    if not divergent_dates:
        reason = (
            "contains no date on which the US and EU DST calendars disagree"
            if not month_has_divergent_dates(year, month)
            else "has no ticks on any date where the US and EU DST calendars disagree"
        )
        return None, (
            f"{year:04d}-{month:02d} {reason}, so us_dst and eu_dst assign the same UTC "
            "instant to every stamp in this file; no determination is needed"
        )

    raise IntegrityError(
        f"{year:04d}-{month:02d} has ticks on "
        f"{len(divergent_dates)} date(s) where the US and EU DST calendars disagree "
        f"({divergent_dates[0]} .. {divergent_dates[-1]}), so its stamp clock matters, "
        f"but nothing in the file reads it{suffix or ''} -- cannot determine the stamp "
        "clock, and guessing would shift those ticks by an hour"
    )


def _read_raw_csv(path: Path) -> pl.DataFrame:
    try:
        return pl.read_csv(
            path,
            has_header=False,
            new_columns=["ts_raw", "bid_s", "ask_s", "volume_s"],
            schema_overrides={
                "ts_raw": pl.Utf8,
                "bid_s": pl.Utf8,
                "ask_s": pl.Utf8,
                "volume_s": pl.Utf8,
            },
        )
    except pl.exceptions.PolarsError as exc:
        raise IntegrityError(f"{path}: failed to read as CSV: {exc}") from exc


def parse_histdata_csv(
    path: Path,
    instrument: str,
    expected_year: int,
    expected_month: int,
) -> HistDataMonth:
    """Parse one HistData monthly tick CSV into a HistDataMonth.

    Row format (no header): `YYYYMMDD HHMMSSmmm,bid,ask,volume`. Volume
    is always 0 in HistData's ASCII export (broker-specific, stripped) --
    the returned bid/ask arrays carry no volume at all; callers writing
    to the tick schema set bid_volume/ask_volume to null, never 0.0."""
    source_sha256 = sha256_file(path)
    raw = _read_raw_csv(path)
    n = raw.height
    if n == 0:
        raise IntegrityError(f"{path}: empty file (no tick rows)")

    ts_raw = raw["ts_raw"]
    valid_format = ts_raw.str.contains(_TS_RE.pattern)
    if not valid_format.all():
        bad_idx = int(np.nonzero(~valid_format.to_numpy())[0][0])
        raise IntegrityError(
            f"{path}: malformed timestamp at line {bad_idx + 1}: {ts_raw[bad_idx]!r} "
            "(expected 'YYYYMMDD HHMMSSmmm')"
        )

    year_i = ts_raw.str.slice(0, 4).cast(pl.Int32, strict=False)
    month_i = ts_raw.str.slice(4, 2).cast(pl.Int32, strict=False)
    day_i = ts_raw.str.slice(6, 2).cast(pl.Int32, strict=False)
    hour_i = ts_raw.str.slice(9, 2).cast(pl.Int32, strict=False)
    minute_i = ts_raw.str.slice(11, 2).cast(pl.Int32, strict=False)
    second_i = ts_raw.str.slice(13, 2).cast(pl.Int32, strict=False)
    ms_i = ts_raw.str.slice(15, 3).cast(pl.Int32, strict=False)

    for name, col in (
        ("year", year_i),
        ("month", month_i),
        ("day", day_i),
        ("hour", hour_i),
        ("minute", minute_i),
        ("second", second_i),
        ("millisecond", ms_i),
    ):
        null_mask = col.is_null()
        if null_mask.any():
            bad_idx = int(np.nonzero(null_mask.to_numpy())[0][0])
            raise IntegrityError(
                f"{path}: malformed {name} field at line {bad_idx + 1}: {ts_raw[bad_idx]!r}"
            )

    wrong_month_mask = (year_i != expected_year) | (month_i != expected_month)
    if wrong_month_mask.any():
        bad_idx = int(np.nonzero(wrong_month_mask.to_numpy())[0][0])
        raise IntegrityError(
            f"{path}: row at line {bad_idx + 1} has date {year_i[bad_idx]:04d}-"
            f"{month_i[bad_idx]:02d}-{day_i[bad_idx]:02d}, expected "
            f"{expected_year:04d}-{expected_month:02d} (filename/content mismatch)"
        )

    col2 = raw["bid_s"].cast(pl.Float64, strict=False)
    col3 = raw["ask_s"].cast(pl.Float64, strict=False)
    for name, col in (("column 2 (price)", col2), ("column 3 (price)", col3)):
        null_mask = col.is_null()
        if null_mask.any():
            bad_idx = int(np.nonzero(null_mask.to_numpy())[0][0])
            raise IntegrityError(f"{path}: malformed {name} value at line {bad_idx + 1}")

    col2_arr = col2.to_numpy()
    col3_arr = col3.to_numpy()
    column_order, column_order_evidence = detect_column_order(col2_arr, col3_arr)
    if column_order == "bid_ask":
        bid_arr, ask_arr = col2_arr, col3_arr
    else:
        bid_arr, ask_arr = col3_arr, col2_arr

    # Sanity net, not the primary defence: detect_column_order already
    # requires >=99% consistency, so this should fire only on a genuine
    # anomaly within an otherwise-consistent file (never seen in real
    # HistData exports at this point), not on a systematic column swap --
    # that case is now caught earlier, with a clear diagnosis, by
    # detect_column_order itself.
    # The test is bid > ask, NOT bid >= ask. A zero spread is unusual but
    # real, and detect_column_order's own threshold is built around that
    # fact -- its 1% tolerance exists precisely to absorb "genuine
    # zero-spread thin quotes (observed: 5 rows out of 554,877 in a clean
    # 2009-11 file)". Rejecting bid == ask here contradicted that and made
    # 2009-11 unimportable: the real file has exactly those 5 equal-price
    # rows and zero rows with bid > ask. An actual inversion is still
    # corruption and still fails.
    bad_spread = np.nonzero(bid_arr > ask_arr)[0]
    if len(bad_spread):
        bad_idx = int(bad_spread[0])
        raise IntegrityError(
            f"{path}: bid > ask at line {bad_idx + 1} after column-order correction "
            f"({column_order}) (bid={bid_arr[bad_idx]}, ask={ask_arr[bad_idx]}) -- "
            "an isolated inversion within an otherwise-consistent file, treated as "
            "corruption"
        )

    y_arr = year_i.to_numpy()
    mo_arr = month_i.to_numpy()
    d_arr = day_i.to_numpy()
    h_arr = hour_i.to_numpy()
    mi_arr = minute_i.to_numpy()
    s_arr = second_i.to_numpy()
    ms_arr = ms_i.to_numpy()

    date_key = y_arr * 10_000 + mo_arr * 100 + d_arr
    unique_keys, date_index = np.unique(date_key, return_inverse=True)
    date_index = date_index.reshape(-1)

    midnight_ns = np.array(
        [_naive_ns(int(k) // 10_000, (int(k) // 100) % 100, int(k) % 100) for k in unique_keys],
        dtype=np.int64,
    )
    naive_local_ns = (
        midnight_ns[date_index]
        + h_arr.astype(np.int64) * NS_PER_HOUR
        + mi_arr.astype(np.int64) * NS_PER_MINUTE
        + s_arr.astype(np.int64) * NS_PER_SECOND
        + ms_arr.astype(np.int64) * 1_000_000
    )

    # Gross ordering corruption is checked BEFORE detection: no stamp
    # clock, on any calendar, steps backwards more than once in a month,
    # so a file that does is corrupt whatever its clock -- and saying so
    # is far more useful than the "cannot determine the stamp clock" the
    # detector would otherwise report for it.
    backward = np.nonzero(np.diff(naive_local_ns) < 0)[0]
    if len(backward) > 1:
        first = int(backward[0])
        raise IntegrityError(
            f"{path}: timestamps not ascending -- {len(backward)} backward steps. "
            "A daylight-saving fall-back accounts for exactly one; more than one is "
            f"corruption. First at line {first + 2}: "
            f"{_stamp_dt(int(naive_local_ns[first])):%Y-%m-%d %H:%M:%S} -> "
            f"{_stamp_dt(int(naive_local_ns[first + 1])):%Y-%m-%d %H:%M:%S}"
        )

    stamp_clock, evidence = detect_stamp_clock(
        naive_local_ns, expected_year, expected_month
    )

    # The ascending check comes AFTER detection, because whether a
    # backward step is corruption or a legitimate fall-back depends on
    # which clock the file is on: an eu_dst October file re-uses an hour
    # it has already stamped, exactly once, at a known instant. Every
    # other file must be strictly ascending.
    _check_stamp_order(naive_local_ns, path, stamp_clock, expected_year, expected_month)

    if stamp_clock == "eu_dst":
        ts_utc_ns, clock_note = _convert_eu_dst(
            naive_local_ns, expected_year, expected_month, path
        )
    else:
        # us_dst, or a month with no divergent date (where us_dst and
        # eu_dst agree on every stamp, so either conversion is correct
        # and America/New_York is used as the canonical one).
        ts_utc_ns, correction_count = _convert_ny_local(
            unique_keys, date_index, naive_local_ns, y_arr, mo_arr, d_arr, h_arr, mi_arr, s_arr, ms_arr
        )
        clock_note = ""
        if correction_count > _AMBIGUOUS_CORRECTION_WARN_THRESHOLD:
            clock_note = (
                f"; WARNING: {correction_count} rows required per-row ambiguous/"
                "non-existent local-time resolution (fold_policy='later') -- this "
                "should be rare (US DST transitions fall inside the weekend closure); "
                "worth investigating"
            )
    evidence += clock_note

    return HistDataMonth(
        instrument=instrument,
        year=expected_year,
        month=expected_month,
        stamp_clock=stamp_clock,
        stamp_clock_evidence=evidence,
        column_order=column_order,
        column_order_evidence=column_order_evidence,
        ts_utc_ns=ts_utc_ns.astype(np.int64),
        bid=bid_arr,
        ask=ask_arr,
        row_count=n,
        source_path=path,
        source_sha256=source_sha256,
    )


def _eu_switch_stamps(year: int, month: int) -> list[tuple[str, int, int, int]]:
    """The eu_dst clock switches, if any, whose discontinuity lands inside
    (year, month). Each is (kind, boundary_utc_ns, stamp_before, stamp_after)
    where the two stamp values are what the file's own clock reads on
    either side of the boundary."""
    spring, autumn = eu_dst_switch_utc_ns(year)
    out = []
    for kind, boundary, off_before, off_after in (
        ("spring", spring, _UTC_MINUS_5, _UTC_MINUS_4),
        ("autumn", autumn, _UTC_MINUS_4, _UTC_MINUS_5),
    ):
        # the discontinuity is stamped on whichever calendar day the
        # before-side stamp falls on
        before_stamp = boundary + off_before
        d = _stamp_dt(before_stamp).date()
        if (d.year, d.month) == (year, month):
            out.append((kind, boundary, before_stamp, boundary + off_after))
    return out


def _check_stamp_order(
    naive_local_ns: np.ndarray,
    path: Path,
    stamp_clock: StampClock | None,
    year: int,
    month: int,
) -> None:
    """Every file must be ascending, with exactly one exception: an
    eu_dst file containing the autumn switch falls back an hour and so
    re-uses stamps it has already emitted. That one backward step is
    permitted only if it is where the rule says it must be -- anything
    else is corruption and fails."""
    backward = np.nonzero(np.diff(naive_local_ns) < 0)[0]
    if len(backward) == 0:
        return

    allowed = [s for s in _eu_switch_stamps(year, month) if s[0] == "autumn"]
    if stamp_clock == "eu_dst" and len(backward) == 1 and allowed:
        _kind, _boundary, stamp_before, stamp_after = allowed[0]
        idx = int(backward[0])
        if (
            abs(int(naive_local_ns[idx]) - stamp_before) <= _SWITCH_TOLERANCE_NS
            and abs(int(naive_local_ns[idx + 1]) - stamp_after) <= _SWITCH_TOLERANCE_NS
        ):
            return

    bad_idx = int(backward[0])
    detail = (
        f"{len(backward)} backward step(s); first at line {bad_idx + 2}, "
        f"{_stamp_dt(int(naive_local_ns[bad_idx])):%Y-%m-%d %H:%M:%S} -> "
        f"{_stamp_dt(int(naive_local_ns[bad_idx + 1])):%Y-%m-%d %H:%M:%S}"
    )
    if allowed and stamp_clock == "eu_dst":
        _k, _b, sb, sa = allowed[0]
        detail += (
            f"; the eu_dst autumn fall-back for this month is a single step "
            f"{_stamp_dt(sb):%Y-%m-%d %H:%M:%S} -> {_stamp_dt(sa):%Y-%m-%d %H:%M:%S}"
        )
    raise IntegrityError(f"{path}: timestamps not ascending -- {detail}")


def _convert_eu_dst(
    naive_local_ns: np.ndarray,
    year: int,
    month: int,
    path: Path,
) -> tuple[np.ndarray, str]:
    """Convert eu_dst stamps to UTC.

    The offset is +5h (UTC-5) in the EU winter period and +4h (UTC-4) in
    the EU summer period, changing at the instants eu_dst_switch_utc_ns
    gives. Rows are split by POSITION IN THE FILE rather than by stamp
    value, because at the autumn fall-back an hour of stamp values occurs
    twice and only file order distinguishes the two passes -- exactly the
    situation a wall-clock-based fold policy cannot resolve.

    The switch position is not assumed: it is located in the file and
    checked against where the rule says it must be, so a file that does
    not actually change clock where expected fails rather than having an
    hour of its ticks silently shifted."""
    switches = _eu_switch_stamps(year, month)
    offset = np.full(
        len(naive_local_ns), _eu_clock_offset_ns(date(year, month, 1)), dtype=np.int64
    )
    notes: list[str] = []

    lo, hi = int(naive_local_ns[0]), int(naive_local_ns[-1])
    for kind, boundary, stamp_before, stamp_after in switches:
        skipped_lo = min(stamp_before, stamp_after)
        skipped_hi = max(stamp_before, stamp_after)
        if hi <= skipped_lo:
            # the file ends before this switch; its opening offset stands
            continue
        if lo >= skipped_hi:
            # the file begins after this switch; the post-switch offset
            # applies to every row, including the first
            offset[:] = _UTC_MINUS_4 if kind == "spring" else _UTC_MINUS_5
            notes.append(
                f"{kind} clock switch precedes this file's first tick; the "
                "post-switch offset applies throughout"
            )
            continue

        if kind == "spring":
            # forward jump: a one-hour hole, so stamp value alone splits it
            idx = int(np.searchsorted(naive_local_ns, stamp_after, side="left"))
        else:
            # fall-back: an hour of stamp VALUES occurs twice, so only
            # position in the file distinguishes the two passes
            back = np.nonzero(np.diff(naive_local_ns) < 0)[0]
            if len(back) != 1:
                raise IntegrityError(
                    f"{path}: an eu_dst October file falls back exactly once, leaving "
                    f"exactly one backward stamp step; this file has {len(back)}"
                )
            idx = int(back[0]) + 1

        observed_before = int(naive_local_ns[idx - 1])
        observed_after = int(naive_local_ns[idx])
        if (
            abs(observed_before - stamp_before) > _SWITCH_TOLERANCE_NS
            or abs(observed_after - stamp_after) > _SWITCH_TOLERANCE_NS
        ):
            raise IntegrityError(
                f"{path}: eu_dst {kind} switch is not where the rule says it is. "
                f"Expected the clock to change between stamps "
                f"{_stamp_dt(stamp_before):%Y-%m-%d %H:%M:%S} and "
                f"{_stamp_dt(stamp_after):%Y-%m-%d %H:%M:%S} (00:00 UTC on the Monday "
                f"after the Europe/London change); the file's own ticks there are "
                f"{_stamp_dt(observed_before):%Y-%m-%d %H:%M:%S} -> "
                f"{_stamp_dt(observed_after):%Y-%m-%d %H:%M:%S}"
            )

        offset[idx:] = _UTC_MINUS_4 if kind == "spring" else _UTC_MINUS_5
        notes.append(
            f"{kind} clock switch verified at row {idx} "
            f"({_stamp_dt(observed_before):%Y-%m-%d %H:%M:%S} -> "
            f"{_stamp_dt(observed_after):%Y-%m-%d %H:%M:%S}, "
            f"boundary {_stamp_dt(boundary):%Y-%m-%d %H:%M:%S} UTC)"
        )

    note = ("; " + "; ".join(notes)) if notes else ""
    return naive_local_ns - offset, note


def _convert_ny_local(
    unique_keys: np.ndarray,
    date_index: np.ndarray,
    naive_local_ns: np.ndarray,
    y_arr: np.ndarray,
    mo_arr: np.ndarray,
    d_arr: np.ndarray,
    h_arr: np.ndarray,
    mi_arr: np.ndarray,
    s_arr: np.ndarray,
    ms_arr: np.ndarray,
) -> tuple[np.ndarray, int]:
    """Vectorised America/New_York local-to-UTC conversion. A per-date
    offset (probed once at noon per unique calendar date -- at most ~31
    jarvis.timeengine calls per month, never per row) handles every row
    except those falling on a genuine DST transition date, where the
    offset itself is not constant across the day. Those rows (identified
    via jarvis.timeengine.is_ambiguous/is_nonexistent, never guessed) are
    individually corrected via local_to_utc_ns(fold_policy="later") --
    in practice this should be a small number of rows, since the
    transition instant (02:00 NY) falls inside the weekend closure."""
    dates = [date(int(k) // 10_000, (int(k) // 100) % 100, int(k) % 100) for k in unique_keys]
    offsets = np.array([_ny_offset_ns(d) for d in dates], dtype=np.int64)

    ts_utc_ns = naive_local_ns - offsets[date_index]

    correction_count = 0
    for i, d in enumerate(dates):
        # A transition date is one where some local time on it is
        # ambiguous or non-existent -- probe the two hours (01:xx, 02:xx)
        # that bracket every historical US transition.
        if not (is_ambiguous(d, time(1, 30), "America/New_York") or is_nonexistent(d, time(2, 30), "America/New_York")):
            continue
        rows = np.nonzero(date_index == i)[0]
        for row in rows:
            t = time(int(h_arr[row]), int(mi_arr[row]), int(s_arr[row]), int(ms_arr[row]) * 1000)
            if is_ambiguous(d, t, "America/New_York") or is_nonexistent(d, t, "America/New_York"):
                ts_utc_ns[row] = int(local_to_utc_ns(d, t, "America/New_York", "later"))
                correction_count += 1

    return ts_utc_ns, correction_count
