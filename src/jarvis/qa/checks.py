"""Individual QA check implementations: tick-level, fetch-log/filesystem,
and bar-level. `report.run_checks` orchestrates these into one QAReport.

Severity/Finding are defined here (not report.py) because every check
function constructs Finding values directly.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal

import numpy as np
import polars as pl

from jarvis.core.types import Nanos
from jarvis.ingest.fetch_log import FetchLogEntry
from jarvis.ingest.parse import TickArrays
from jarvis.timeengine import (
    NS_PER_MINUTE,
    from_utc_ns,
    is_weekend_gap,
    trading_day,
    trading_day_bounds,
)

Severity = Literal["ERROR", "WARNING", "INFO"]

_SAMPLE_LIMIT = 10
_JUMP_WINDOW = 1000  # W-02: trailing tick count for the rolling stdev
_WEEKEND_BUFFER_NS = 5 * NS_PER_MINUTE  # W-03: buffer on is_weekend_gap's exact boundary
_EXTREME_SPREAD_MULTIPLE = 20  # W-04
_EXTREME_SPREAD_MIN_BUCKET = 100  # W-04: minimum bars per hour-of-week bucket
_THIN_DAY_BASELINE = 20  # W-05: trailing trading days required
_DST_DEVIATION_FRACTION = 0.4  # I-01


@dataclass(frozen=True, slots=True)
class Finding:
    check_id: str
    check_name: str
    severity: Severity
    year: int | None
    count: int
    detail: str
    sample: tuple[str, ...]


def _iso(ns: int) -> str:
    return datetime.fromtimestamp(ns // 1_000_000_000, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _append_sample(target: list[str], indices, formatter) -> None:
    remaining = _SAMPLE_LIMIT - len(target)
    if remaining <= 0:
        return
    for idx in indices[:remaining]:
        target.append(formatter(int(idx)))


# ---------------------------------------------------------------------------
# Tick-level checks: E-01, E-02, E-03, W-01, W-02, W-03, I-02
# ---------------------------------------------------------------------------


class TickChecksAccumulator:
    """Accumulates tick-level check state hour by hour. Holds only scalar
    counters and small sample lists -- never raw tick arrays -- so the
    caller's "one hour of ticks in memory at a time" discipline (WP-005)
    is preserved here too."""

    def __init__(self) -> None:
        self.e01_negative = 0
        self.e01_zero = 0
        self.e01_sample: list[str] = []
        self.e02_count = 0
        self.e02_sample: list[str] = []
        self.e03_count = 0
        self.e03_sample: list[str] = []
        self.w01_count = 0
        self.w01_sample: list[str] = []
        self.w02_count = 0
        self.w02_sample: list[str] = []
        self.w03_count = 0
        self.w03_sample: list[str] = []
        self.i02_count = 0
        self.i02_sample: list[str] = []

    def add_hour(self, ticks: TickArrays, hour_ns: Nanos) -> None:
        n = len(ticks.ts_utc_ns)
        if n == 0:
            return
        ts, bid, ask = ticks.ts_utc_ns, ticks.bid, ticks.ask

        self._check_spread(ts, bid, ask)
        self._check_price(ts, bid, ask)
        self._check_reversal(ts)
        self._check_duplicate(ts, bid, ask)
        self._check_jump(ts, bid, ask)
        self._check_weekend(hour_ns, ts)
        self._check_zero_volume(hour_ns, ticks.bid_volume, ticks.ask_volume)

    # E-01 -------------------------------------------------------------
    def _check_spread(self, ts: np.ndarray, bid: np.ndarray, ask: np.ndarray) -> None:
        spread = ask - bid
        neg_mask = spread < 0
        zero_mask = spread == 0
        self.e01_negative += int(neg_mask.sum())
        self.e01_zero += int(zero_mask.sum())
        bad_idx = np.nonzero(neg_mask | zero_mask)[0]
        _append_sample(
            self.e01_sample, bad_idx, lambda i: f"{_iso(int(ts[i]))} spread={spread[i]:.6f}"
        )

    # E-02 -------------------------------------------------------------
    def _check_price(self, ts: np.ndarray, bid: np.ndarray, ask: np.ndarray) -> None:
        bad_mask = (bid <= 0) | (ask <= 0)
        self.e02_count += int(bad_mask.sum())
        bad_idx = np.nonzero(bad_mask)[0]
        _append_sample(
            self.e02_sample,
            bad_idx,
            lambda i: f"{_iso(int(ts[i]))} bid={bid[i]:.6f} ask={ask[i]:.6f}",
        )

    # E-03 -------------------------------------------------------------
    def _check_reversal(self, ts: np.ndarray) -> None:
        """Strict decrease only -- two ticks sharing a millisecond is not a
        reversal (Correction 3 / acceptance criterion 3)."""
        if len(ts) < 2:
            return
        reversal_mask = np.diff(ts) < 0
        self.e03_count += int(reversal_mask.sum())
        bad_idx = np.nonzero(reversal_mask)[0]
        _append_sample(
            self.e03_sample,
            bad_idx,
            lambda i: f"{_iso(int(ts[i]))} -> {_iso(int(ts[i + 1]))}",
        )

    # W-01 -------------------------------------------------------------
    def _check_duplicate(self, ts: np.ndarray, bid: np.ndarray, ask: np.ndarray) -> None:
        if len(ts) < 2:
            return
        dup_mask = (ts[1:] == ts[:-1]) & (bid[1:] == bid[:-1]) & (ask[1:] == ask[:-1])
        self.w01_count += int(dup_mask.sum())
        bad_idx = np.nonzero(dup_mask)[0]
        _append_sample(
            self.w01_sample, bad_idx, lambda i: f"{_iso(int(ts[i + 1]))} duplicates prior tick"
        )

    # W-02 -------------------------------------------------------------
    def _check_jump(self, ts: np.ndarray, bid: np.ndarray, ask: np.ndarray) -> None:
        """Unrealistic jump: |mid move| > 10x the trailing-1000-tick stdev
        of mid moves, computed WITHIN this hour only (approximation, not an
        oversight: carrying the rolling window across an hour boundary
        would couple every hour's result to whether its predecessor was
        fetched, making the check non-deterministic under partial ingest).
        The first _JUMP_WINDOW ticks of the hour never have a full trailing
        window and are skipped -- not flagged either way.

        Rolling stdev via cumulative sum/sum-of-squares (population stdev,
        ddof=0): O(n) vectorised, no per-tick Python loop, since a naive
        per-window recomputation would be O(n * window) and dominate
        runtime at full-dataset scale."""
        n = len(ts)
        if n <= _JUMP_WINDOW + 1:
            return
        mid = (bid.astype(np.float64) + ask.astype(np.float64)) / 2.0
        diffs = np.diff(mid)
        m = len(diffs)
        if m <= _JUMP_WINDOW:
            return

        cumsum = np.concatenate(([0.0], np.cumsum(diffs)))
        cumsum2 = np.concatenate(([0.0], np.cumsum(diffs * diffs)))

        j = np.arange(_JUMP_WINDOW, m)  # diff-index of the move being evaluated
        window_sum = cumsum[j] - cumsum[j - _JUMP_WINDOW]
        window_sum2 = cumsum2[j] - cumsum2[j - _JUMP_WINDOW]
        window_mean = window_sum / _JUMP_WINDOW
        window_var = np.maximum(window_sum2 / _JUMP_WINDOW - window_mean * window_mean, 0.0)
        window_std = np.sqrt(window_var)

        current_move = diffs[j]
        flagged = np.abs(current_move) > 10 * window_std

        self.w02_count += int(flagged.sum())
        flagged_diff_idx = j[flagged]
        tick_idx = flagged_diff_idx + 1  # diffs[k] = mid[k+1] - mid[k]
        _append_sample(
            self.w02_sample,
            tick_idx,
            lambda i: f"{_iso(int(ts[i]))} move={mid[i] - mid[i - 1]:.6f}",
        )

    # W-03 -------------------------------------------------------------
    def _check_weekend(self, hour_ns: Nanos, ts: np.ndarray) -> None:
        """Weekend activity, Fri 17:05 NY - Sun 16:55 NY: a 5-minute buffer
        on top of timeengine.is_weekend_gap's exact 17:00 boundaries, so
        genuine boundary-adjacent activity is not flagged. The boundary
        itself is never reimplemented here -- the buffer is applied only
        by comparing is_weekend_gap at ts and at ts +/- 5 minutes."""
        if not _hour_may_touch_weekend(hour_ns):
            return
        flagged_idx = []
        for i in range(len(ts)):
            t = int(ts[i])
            if (
                is_weekend_gap(Nanos(t))
                and is_weekend_gap(Nanos(t - _WEEKEND_BUFFER_NS))
                and is_weekend_gap(Nanos(t + _WEEKEND_BUFFER_NS))
            ):
                flagged_idx.append(i)
        self.w03_count += len(flagged_idx)
        _append_sample(
            self.w03_sample, flagged_idx, lambda i: f"{_iso(int(ts[i]))} inside weekend gap"
        )

    # I-02 -------------------------------------------------------------
    def _check_zero_volume(
        self, hour_ns: Nanos, bid_volume: np.ndarray, ask_volume: np.ndarray
    ) -> None:
        if np.all(bid_volume == 0.0) and np.all(ask_volume == 0.0):
            self.i02_count += 1
            _append_sample(self.i02_sample, [0], lambda _i: f"{_iso(int(hour_ns))}")

    def finalize(self) -> list[Finding]:
        findings: list[Finding] = []
        total_bad = self.e01_negative + self.e01_zero
        if total_bad:
            findings.append(
                Finding(
                    check_id="E-01",
                    check_name="Non-positive spread",
                    severity="ERROR",
                    year=None,
                    count=total_bad,
                    detail=(
                        f"{self.e01_negative} strictly negative (inverted quote, "
                        f"likely a decode error), {self.e01_zero} exactly zero "
                        "(likely a thin/stale quote)"
                    ),
                    sample=tuple(self.e01_sample),
                )
            )
        if self.e02_count:
            findings.append(
                Finding(
                    "E-02",
                    "Non-positive price",
                    "ERROR",
                    None,
                    self.e02_count,
                    f"{self.e02_count} ticks with bid <= 0 or ask <= 0",
                    tuple(self.e02_sample),
                )
            )
        if self.e03_count:
            findings.append(
                Finding(
                    "E-03",
                    "Timestamp reversal",
                    "ERROR",
                    None,
                    self.e03_count,
                    f"{self.e03_count} strict timestamp decreases within a source file",
                    tuple(self.e03_sample),
                )
            )
        if self.w01_count:
            findings.append(
                Finding(
                    "W-01",
                    "Duplicate tick",
                    "WARNING",
                    None,
                    self.w01_count,
                    f"{self.w01_count} consecutive ticks identical in (ts, bid, ask)",
                    tuple(self.w01_sample),
                )
            )
        if self.w02_count:
            findings.append(
                Finding(
                    "W-02",
                    "Unrealistic jump",
                    "WARNING",
                    None,
                    self.w02_count,
                    (
                        f"{self.w02_count} mid-price moves exceeding 10x the trailing "
                        f"{_JUMP_WINDOW}-tick stdev (computed within-hour only)"
                    ),
                    tuple(self.w02_sample),
                )
            )
        if self.w03_count:
            findings.append(
                Finding(
                    "W-03",
                    "Weekend activity",
                    "WARNING",
                    None,
                    self.w03_count,
                    f"{self.w03_count} ticks between Fri 17:05 NY and Sun 16:55 NY",
                    tuple(self.w03_sample),
                )
            )
        if self.i02_count:
            findings.append(
                Finding(
                    "I-02",
                    "Volume all-zero",
                    "INFO",
                    None,
                    self.i02_count,
                    f"{self.i02_count} hours reporting 0.0 for both bid and ask volume",
                    tuple(self.i02_sample),
                )
            )
        return findings


def _hour_may_touch_weekend(hour_ns: Nanos) -> bool:
    """Cheap UTC-only pre-filter to skip is_weekend_gap's per-tick zoneinfo
    cost for the large majority of hours that are obviously nowhere near
    the weekend gap. Deliberately generous (covers both EST -5 and EDT -4)
    so it can only over-include, never under-include, hours that need the
    real per-tick check. Not a reimplementation of the boundary itself --
    is_weekend_gap is still the sole source of truth for any hour that
    passes this filter."""
    dt = datetime.fromtimestamp(hour_ns // 1_000_000_000, tz=timezone.utc)
    weekday = dt.isoweekday()  # Mon=1 .. Sun=7
    if weekday in (6, 7):  # Sat, Sun UTC
        return True
    if weekday == 5 and dt.hour >= 20:  # Friday evening UTC
        return True
    return False


# ---------------------------------------------------------------------------
# Fetch-log / filesystem checks: E-04, W-06, E-05, E-06
# ---------------------------------------------------------------------------


_MISSING_HOUR_ERROR_THRESHOLD = 0.005  # 0.5% of a year's expected trading-week hours


def _year_of(hour_ns: Nanos) -> int:
    return datetime.fromtimestamp(hour_ns // 1_000_000_000, tz=timezone.utc).year


class FetchLogChecksAccumulator:
    """Accumulates E-04/W-06 (missing hours, per year), E-05 (malformed
    blob), and E-06 (fetch log / filesystem disagreement) across the
    hour-by-hour pass. Filesystem is the source of truth throughout
    (D-038); the fetch log is consulted only to classify *why*."""

    def __init__(self) -> None:
        self.year_missing: dict[int, int] = {}
        self.year_denominator: dict[int, int] = {}
        self.year_missing_sample: dict[int, list[str]] = {}

        self.e05_count = 0
        self.e05_sample: list[str] = []

        self.e06_log_fetched_no_blob = 0
        self.e06_blob_no_log_entry = 0
        self.e06_sample: list[str] = []

    def observe_hour(
        self,
        hour_ns: Nanos,
        blob_exists: bool,
        blob_size: int,
        log_entry: FetchLogEntry | None,
    ) -> None:
        if not is_weekend_gap(hour_ns):
            year = _year_of(hour_ns)
            self.year_denominator[year] = self.year_denominator.get(year, 0) + 1
            if not blob_exists:
                self.year_missing[year] = self.year_missing.get(year, 0) + 1
                lst = self.year_missing_sample.setdefault(year, [])
                if len(lst) < _SAMPLE_LIMIT:
                    lst.append(_iso(int(hour_ns)))

        # E-06(a): log says fetched, filesystem disagrees.
        if log_entry is not None and log_entry.status == "fetched" and not blob_exists:
            self.e06_log_fetched_no_blob += 1
            if len(self.e06_sample) < _SAMPLE_LIMIT:
                self.e06_sample.append(f"{_iso(int(hour_ns))} log=fetched, no blob on disk")

        # E-06(b): non-empty blob with no matching log entry at all.
        if blob_exists and blob_size > 0 and log_entry is None:
            self.e06_blob_no_log_entry += 1
            if len(self.e06_sample) < _SAMPLE_LIMIT:
                self.e06_sample.append(f"{_iso(int(hour_ns))} non-empty blob, no log entry")

    def record_malformed(self, hour_ns: Nanos, error: str) -> None:
        self.e05_count += 1
        if len(self.e05_sample) < _SAMPLE_LIMIT:
            self.e05_sample.append(f"{_iso(int(hour_ns))}: {error}")

    def finalize(self) -> list[Finding]:
        findings: list[Finding] = []

        for year in sorted(self.year_missing):
            missing = self.year_missing[year]
            denominator = self.year_denominator.get(year, 0)
            ratio = (missing / denominator) if denominator else 1.0
            is_error = ratio > _MISSING_HOUR_ERROR_THRESHOLD
            findings.append(
                Finding(
                    check_id="E-04" if is_error else "W-06",
                    check_name=(
                        "Missing hours exceed threshold"
                        if is_error
                        else "Missing hours below threshold"
                    ),
                    severity="ERROR" if is_error else "WARNING",
                    year=year,
                    count=missing,
                    detail=(
                        f"{missing} of {denominator} trading-week hours in {year} "
                        f"(intersected with the requested range) have no raw blob "
                        f"({ratio:.3%}, threshold {_MISSING_HOUR_ERROR_THRESHOLD:.1%})"
                    ),
                    sample=tuple(self.year_missing_sample.get(year, ())),
                )
            )

        if self.e05_count:
            findings.append(
                Finding(
                    "E-05",
                    "Malformed blob",
                    "ERROR",
                    None,
                    self.e05_count,
                    f"{self.e05_count} blobs failed to parse (decompressed length not a "
                    "multiple of 20, or invalid LZMA)",
                    tuple(self.e05_sample),
                )
            )

        e06_total = self.e06_log_fetched_no_blob + self.e06_blob_no_log_entry
        if e06_total:
            findings.append(
                Finding(
                    "E-06",
                    "Fetch log / filesystem disagreement",
                    "ERROR",
                    None,
                    e06_total,
                    (
                        f"{self.e06_log_fetched_no_blob} hours where the fetch log says "
                        f"'fetched' but no blob exists; {self.e06_blob_no_log_entry} hours "
                        "with a non-empty blob and no fetch log entry"
                    ),
                    tuple(self.e06_sample),
                )
            )

        return findings


# ---------------------------------------------------------------------------
# Bar-level checks: W-04, W-05, I-01 (plus I-03/I-04, insufficient-data notes)
# ---------------------------------------------------------------------------


def _trading_days_covering(start_ns: Nanos, end_ns: Nanos) -> list[date]:
    """Every trading-day label whose bounds overlap [start_ns, end_ns),
    including partial boundary days and weekend-only labels (Saturday/
    Sunday labels are entirely inside the weekend closure but are still
    valid trading-day labels -- and a US DST transition always falls
    inside one, see I-01)."""
    day = trading_day(start_ns)
    days: list[date] = []
    while True:
        bounds_start, _bounds_end = trading_day_bounds(day)
        if bounds_start >= end_ns:
            break
        days.append(day)
        day = day + timedelta(days=1)
    return days


def _fully_contained(days: list[date], start_ns: Nanos, end_ns: Nanos) -> list[date]:
    result = []
    for d in days:
        s, e = trading_day_bounds(d)
        if s >= start_ns and e <= end_ns:
            result.append(d)
    return result


def bar_level_checks(
    bars_df: pl.DataFrame, start_ns: Nanos, end_ns: Nanos, thin_day_threshold: float
) -> list[Finding]:
    if bars_df.height == 0:
        return []

    all_days = _trading_days_covering(start_ns, end_ns)
    if not all_days:
        return []

    day_bounds = [trading_day_bounds(d) for d in all_days]
    day_starts = np.array([b[0] for b in day_bounds], dtype=np.int64)
    ts = bars_df["ts_utc_ns"].to_numpy()

    day_idx = np.searchsorted(day_starts, ts, side="right") - 1
    day_idx = np.clip(day_idx, 0, len(all_days) - 1)

    counts_per_day = np.bincount(day_idx, minlength=len(all_days))
    day_to_count = {d: int(counts_per_day[i]) for i, d in enumerate(all_days)}

    findings: list[Finding] = []
    findings.extend(_check_extreme_spread(bars_df, ts, all_days, day_idx))
    findings.extend(
        _check_thin_day(all_days, day_to_count, start_ns, end_ns, thin_day_threshold)
    )
    findings.extend(_check_dst_day(all_days, day_to_count, start_ns, end_ns))
    return findings


def _check_extreme_spread(
    bars_df: pl.DataFrame, ts: np.ndarray, all_days: list[date], day_idx: np.ndarray
) -> list[Finding]:
    """W-04: bar spread_twa > 20x the median for its (weekday, London-local
    hour) bucket, computed over the whole requested range. Buckets with
    fewer than _EXTREME_SPREAD_MIN_BUCKET bars are skipped entirely and
    reported as I-03, rather than producing a threshold from a handful of
    samples."""
    weekday_arr = np.array([d.isoweekday() for d in all_days], dtype=np.int64)
    bucket_weekday = weekday_arr[day_idx]
    # Per-bar Europe/London local hour: a per-bar zoneinfo conversion, the
    # same approach as WP-005's throughput-sensitive paths avoid -- kept
    # simple here since W-04 is not the WP's designated throughput target;
    # flagged as a known hotspot for very large ranges in closing notes.
    bucket_hour = np.array(
        [from_utc_ns(Nanos(int(t)), "Europe/London").hour for t in ts], dtype=np.int64
    )

    df = bars_df.with_columns(
        [
            pl.Series("_bucket_weekday", bucket_weekday),
            pl.Series("_bucket_hour", bucket_hour),
        ]
    )
    bucket_stats = df.group_by(["_bucket_weekday", "_bucket_hour"]).agg(
        [
            pl.col("spread_twa").median().alias("_median_spread"),
            pl.len().alias("_bucket_count"),
        ]
    )
    joined = df.join(bucket_stats, on=["_bucket_weekday", "_bucket_hour"], how="left")

    eligible = joined.filter(pl.col("_bucket_count") >= _EXTREME_SPREAD_MIN_BUCKET)
    flagged = eligible.filter(
        pl.col("spread_twa") > _EXTREME_SPREAD_MULTIPLE * pl.col("_median_spread")
    ).sort("ts_utc_ns")

    skipped = bucket_stats.filter(pl.col("_bucket_count") < _EXTREME_SPREAD_MIN_BUCKET).sort(
        ["_bucket_weekday", "_bucket_hour"]
    )

    findings: list[Finding] = []
    if flagged.height:
        sample = tuple(_iso(int(v)) for v in flagged["ts_utc_ns"].to_list()[:_SAMPLE_LIMIT])
        findings.append(
            Finding(
                "W-04",
                "Extreme spread",
                "WARNING",
                None,
                flagged.height,
                (
                    f"{flagged.height} bars with spread_twa > "
                    f"{_EXTREME_SPREAD_MULTIPLE}x their (weekday, London-hour) bucket "
                    "median"
                ),
                sample,
            )
        )
    if skipped.height:
        sample = tuple(
            f"weekday={row['_bucket_weekday']} hour={row['_bucket_hour']:02d} "
            f"({row['_bucket_count']} bars)"
            for row in skipped.head(_SAMPLE_LIMIT).iter_rows(named=True)
        )
        findings.append(
            Finding(
                "I-03",
                "Extreme spread check skipped (insufficient bucket samples)",
                "INFO",
                None,
                skipped.height,
                (
                    f"{skipped.height} (weekday, London-hour) buckets have fewer than "
                    f"{_EXTREME_SPREAD_MIN_BUCKET} bars in range; W-04 not evaluated for them"
                ),
                sample,
            )
        )
    return findings


def _check_thin_day(
    all_days: list[date],
    day_to_count: dict[date, int],
    start_ns: Nanos,
    end_ns: Nanos,
    thin_day_threshold: float,
) -> list[Finding]:
    """W-05: present-bar count for a weekday trading day < thin_day_threshold
    x the trailing-20-trading-day median, both computed strictly within the
    requested range (D-036: 2006 is a partial year and must not borrow
    history from outside the requested window). Saturday/Sunday
    trading-day labels are always fully inside market closure by
    construction and are excluded -- they would otherwise be flagged every
    single week for a reason that has nothing to do with data quality."""
    weekday_days = sorted(
        d for d in _fully_contained(all_days, start_ns, end_ns) if d.isoweekday() <= 5
    )

    thin_count = 0
    thin_sample: list[str] = []
    baseline_skipped = 0
    baseline_sample: list[str] = []

    for i, d in enumerate(weekday_days):
        if i < _THIN_DAY_BASELINE:
            baseline_skipped += 1
            if len(baseline_sample) < _SAMPLE_LIMIT:
                baseline_sample.append(d.isoformat())
            continue
        baseline_counts = [day_to_count[prior] for prior in weekday_days[i - _THIN_DAY_BASELINE : i]]
        median_count = float(np.median(baseline_counts))
        threshold = thin_day_threshold * median_count
        if day_to_count[d] < threshold:
            thin_count += 1
            if len(thin_sample) < _SAMPLE_LIMIT:
                thin_sample.append(
                    f"{d.isoformat()} ({day_to_count[d]} bars < {threshold:.1f} threshold)"
                )

    findings: list[Finding] = []
    if thin_count:
        findings.append(
            Finding(
                "W-05",
                "Thin trading day",
                "WARNING",
                None,
                thin_count,
                (
                    f"{thin_count} trading days with present-bar count below "
                    f"{thin_day_threshold:.0%} of the trailing {_THIN_DAY_BASELINE}-day median"
                ),
                tuple(thin_sample),
            )
        )
    if baseline_skipped:
        findings.append(
            Finding(
                "I-04",
                "Thin-day check skipped (insufficient baseline)",
                "INFO",
                None,
                baseline_skipped,
                (
                    f"{baseline_skipped} trading days have fewer than "
                    f"{_THIN_DAY_BASELINE} prior trading days of history within the "
                    "requested range; W-05 not evaluated for them"
                ),
                tuple(baseline_sample),
            )
        )
    return findings


def _check_dst_day(
    all_days: list[date], day_to_count: dict[date, int], start_ns: Nanos, end_ns: Nanos
) -> list[Finding]:
    """I-01 (Correction 3): a trading day containing a DST transition is 23
    or 25 hours long, not 24 -- confirmed by trading_day_bounds, not by any
    hardcoded transition date. Flags INFO when the present-bar count
    deviates from the day's own expected-duration-in-minutes by more than
    _DST_DEVIATION_FRACTION. The point is to confirm the time engine and
    the resampler agree about DST-affected days, not to detect a market
    anomaly -- so this never touches ERROR/WARNING severity."""
    candidates = _fully_contained(all_days, start_ns, end_ns)
    deviated: list[str] = []
    dst_day_count = 0

    for d in candidates:
        s, e = trading_day_bounds(d)
        duration_minutes = (e - s) // NS_PER_MINUTE
        if duration_minutes == 1440:
            continue
        dst_day_count += 1
        expected = duration_minutes
        actual = day_to_count.get(d, 0)
        if expected and abs(actual - expected) > _DST_DEVIATION_FRACTION * expected:
            if len(deviated) < _SAMPLE_LIMIT:
                deviated.append(
                    f"{d.isoformat()} expected~{expected}min actual_bars={actual}"
                )

    if not deviated:
        return []
    return [
        Finding(
            "I-01",
            "DST day bar count",
            "INFO",
            None,
            len(deviated),
            (
                f"{len(deviated)} of {dst_day_count} DST-transition trading days in range "
                "have a present-bar count deviating >40% from the day's own expected "
                "duration -- time engine / resampler disagreement on a DST-affected day"
            ),
            tuple(deviated),
        )
    ]
