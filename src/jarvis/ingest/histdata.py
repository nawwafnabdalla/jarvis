"""HistData.com CSV parsing and per-file timezone-convention detection.

THE CRITICAL FINDING (WP-009): HistData's own FAQ claims timestamps are
"EST without daylight saving adjustments." This is WRONG for 2006-2022 --
verified across 14 sample months against three independent anchors (NFP
at 08:30 NY, weekend close at 17:00 NY, the 16:00 London WMR fix's March
DST-divergence shift). Timestamps in that range are actually
`America/New_York` local time, DST-aware, under CORRECT historical US
transition rules (verified against November 2004's pre-2007 regime).
HistData also changed convention between October 2022 and November 2023.

Because of that changeover, this module trusts nothing about convention
except what each file's own content proves: `detect_tz_convention` finds
Friday weekend closes and reads what the file itself says the close
time was, using jarvis.timeengine's ground-truth NY DST rules only to
decide which Fridays are discriminating -- never to assume the file's
own convention. A future file (post-2022) that uses the other convention
fails loudly (a mixed-evidence IntegrityError, or a clean single-
convention detection that simply differs from what came before) rather
than silently shifting every session boundary by an hour.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
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
_GAP_THRESHOLD_NS = NS_PER_HOUR  # "a gap > 1 hour" per spec
_FRIDAY = 4  # Python's date.weekday(): Monday=0 .. Sunday=6
_AMBIGUOUS_CORRECTION_WARN_THRESHOLD = 10  # "a handful" -- see module docstring

TzConvention = Literal["ny_local", "fixed_utc_minus_5"]

_TS_RE = re.compile(r"^\d{8} \d{9}$")


@dataclass(frozen=True, slots=True)
class HistDataMonth:
    instrument: str
    year: int
    month: int
    convention: TzConvention
    convention_evidence: str  # human-readable, goes in the import report
    ts_utc_ns: np.ndarray  # int64, ascending
    bid: np.ndarray  # float64
    ask: np.ndarray  # float64
    row_count: int
    source_path: Path
    source_sha256: str


def _naive_ns(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0, ms: int = 0) -> int:
    """Nanoseconds since the Unix epoch for (y,mo,d,h,mi,s,ms) treated AS
    IF it were UTC -- a naive-arithmetic device, not a real UTC instant.
    Used only for sortable/diffable representation of a local wall-clock
    stamp whose real zone is not yet known (or is being determined)."""
    return (
        int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp()) * NS_PER_SECOND
        + ms * 1_000_000
    )


def _ny_offset_ns(d: date, probe: time = time(12, 0)) -> int:
    """naive_ns(d, probe) - real_utc_ns(d, probe) for America/New_York --
    i.e. how much to SUBTRACT from a naive-as-utc local stamp on this date
    to get the real UTC instant. Probed at noon, which is never itself
    ambiguous or non-existent (US DST transitions occur at 02:00 local)."""
    naive = _naive_ns(d.year, d.month, d.day, probe.hour, probe.minute)
    real_utc = int(local_to_utc_ns(d, probe, "America/New_York", "later"))
    return naive - real_utc


def detect_tz_convention(
    local_stamps: np.ndarray,
    year: int,
    month: int,
    convention_hint: TzConvention | None = None,
) -> tuple[TzConvention, str]:
    """Determine a file's timezone convention from its own content.

    `local_stamps` is the naive-as-UTC nanosecond representation (see
    _naive_ns) of every tick's local wall-clock stamp, ascending.

    Method: find every Friday weekend close (a gap > 1 hour where the
    preceding tick is on a Friday). For Fridays falling in an EDT period
    (determined from jarvis.timeengine's real America/New_York rules,
    never from the file's own content):
      close stamp 16:xx -> ny_local
      close stamp 15:xx -> fixed_utc_minus_5
    EST Fridays are NOT discriminating (both conventions give 16:xx) and
    are ignored rather than counted as evidence.

    Raises IntegrityError if:
      - the file is internally mixed (some EDT Fridays 16:xx, others 15:xx)
      - there are no discriminating Fridays AND no convention was supplied
        by the caller (convention_hint)

    Returns the convention plus a one-line evidence string naming the
    Fridays used and their stamps."""
    n = len(local_stamps)
    ny_local_evidence: list[str] = []
    fixed_evidence: list[str] = []

    if n >= 2:
        gaps = np.diff(local_stamps)
        gap_positions = np.nonzero(gaps > _GAP_THRESHOLD_NS)[0]
        for idx in gap_positions:
            close_ns = int(local_stamps[idx])
            close_dt = datetime.fromtimestamp(close_ns // NS_PER_SECOND, tz=timezone.utc)
            close_date = close_dt.date()
            if close_date.weekday() != _FRIDAY:
                continue

            is_edt = _ny_offset_ns(close_date) == -4 * NS_PER_HOUR
            if not is_edt:
                continue  # EST Friday: not discriminating

            close_hour = close_dt.hour
            close_minute = close_dt.minute
            label = f"{close_date.isoformat()} close={close_hour:02d}:{close_minute:02d}"
            if close_hour == 16:
                ny_local_evidence.append(f"{label} (ny_local)")
            elif close_hour == 15:
                fixed_evidence.append(f"{label} (fixed_utc_minus_5)")
            # any other hour is not a recognisable weekend-close pattern
            # for either convention and is silently not counted -- it is
            # not evidence of anything, not a contradiction.

    if ny_local_evidence and fixed_evidence:
        raise IntegrityError(
            f"{year:04d}-{month:02d}: internally mixed timezone convention -- "
            f"EDT Fridays reading ny_local: {'; '.join(ny_local_evidence)}; "
            f"EDT Fridays reading fixed_utc_minus_5: {'; '.join(fixed_evidence)}"
        )

    if ny_local_evidence:
        return "ny_local", f"{len(ny_local_evidence)} discriminating EDT Friday(s): " + "; ".join(
            ny_local_evidence
        )
    if fixed_evidence:
        return (
            "fixed_utc_minus_5",
            f"{len(fixed_evidence)} discriminating EDT Friday(s): " + "; ".join(fixed_evidence),
        )

    if convention_hint is not None:
        return (
            convention_hint,
            f"no discriminating EDT Friday found in {year:04d}-{month:02d}; "
            f"used caller-supplied convention_hint={convention_hint!r}",
        )

    raise IntegrityError(
        f"{year:04d}-{month:02d}: no discriminating EDT Friday found (file may be too "
        "short, entirely within an EST period, or missing weekend gaps) and no "
        "convention_hint was supplied -- cannot determine timezone convention"
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
    convention_hint: TzConvention | None = None,
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

    bid = raw["bid_s"].cast(pl.Float64, strict=False)
    ask = raw["ask_s"].cast(pl.Float64, strict=False)
    for name, col in (("bid", bid), ("ask", ask)):
        null_mask = col.is_null()
        if null_mask.any():
            bad_idx = int(np.nonzero(null_mask.to_numpy())[0][0])
            raise IntegrityError(f"{path}: malformed {name} value at line {bad_idx + 1}")

    bid_arr = bid.to_numpy()
    ask_arr = ask.to_numpy()
    bad_spread = np.nonzero(bid_arr >= ask_arr)[0]
    if len(bad_spread):
        bad_idx = int(bad_spread[0])
        raise IntegrityError(
            f"{path}: bid >= ask at line {bad_idx + 1} (bid={bid_arr[bad_idx]}, "
            f"ask={ask_arr[bad_idx]}) -- never seen in genuine HistData exports, "
            "treated as corruption"
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

    non_ascending = np.nonzero(np.diff(naive_local_ns) < 0)[0]
    if len(non_ascending):
        bad_idx = int(non_ascending[0]) + 1
        raise IntegrityError(
            f"{path}: timestamps not ascending at line {bad_idx + 1} "
            f"(local stamp decreased from the previous row)"
        )

    convention, evidence = detect_tz_convention(
        naive_local_ns, expected_year, expected_month, convention_hint
    )

    if convention == "fixed_utc_minus_5":
        ts_utc_ns = naive_local_ns + 5 * NS_PER_HOUR
    else:
        ts_utc_ns, correction_count = _convert_ny_local(
            unique_keys, date_index, naive_local_ns, y_arr, mo_arr, d_arr, h_arr, mi_arr, s_arr, ms_arr
        )
        if correction_count > _AMBIGUOUS_CORRECTION_WARN_THRESHOLD:
            evidence += (
                f"; WARNING: {correction_count} rows required per-row ambiguous/"
                "non-existent local-time resolution (fold_policy='later') -- this "
                "should be rare (DST transitions fall inside the weekend closure); "
                "worth investigating"
            )

    return HistDataMonth(
        instrument=instrument,
        year=expected_year,
        month=expected_month,
        convention=convention,
        convention_evidence=evidence,
        ts_utc_ns=ts_utc_ns.astype(np.int64),
        bid=bid_arr,
        ask=ask_arr,
        row_count=n,
        source_path=path,
        source_sha256=source_sha256,
    )


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
