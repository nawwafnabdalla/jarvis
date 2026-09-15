"""R2 -- London range conditional on pre-London range percentile
(Technical Bible Part 2 SS G.1.2).

Question: does a compressed Asian range associate with a larger or
smaller London range?
Calculation: bucket trading days by `pre_london_range_pct(60)` into
quintiles; report the distribution of `london_range / atr_bars(1440)`
per bucket; per year and pooled.
Uncertainty: bootstrap CI on each bucket median; explicit n per bucket,
pooled and per year.
Restriction: the per-year table must sit adjacent to the pooled table
(handled by `reporting.describe_r2`, not here).

Two things the literal spec text leaves for this module to resolve
(confirmed with the user before this file was written, WP-021):

1. "Quintile" is a defined term -- five EQUAL-COUNT groups, not five
   equal-width bands of the percentile's own value range. Bucket
   boundaries are computed ONCE, pooled across the whole report period,
   then reused unchanged for the per-year breakdown -- re-bucketing per
   year would make "bottom quintile" locally relative and defeat the
   Restriction's own artefact-check (comparing the SAME bucket
   definition across years).
2. `pre_london_range_pct(60)` is a genuinely DISCRETE quantity -- exactly
   61 possible values (`k/60`, k=0..60; see `pre_london_range_pct_compute`
   in `features/library.py`), confirmed by reading that function rather
   than assuming a roughly-continuous variable. With ~2,000 pooled
   eligible days and ~33 days per unique value on average against a
   ~400-day bucket target, exact ties at a would-be quintile boundary are
   the normal condition, not an edge case. `_bucket_quintiles` below never
   splits a group of days sharing an identical value across two buckets;
   it fills buckets in ascending value order and closes a bucket once its
   running share reaches ~n/5, at a value-group boundary. The first four
   buckets get this treatment; the fifth necessarily absorbs whatever is
   left after the fourth closes, and can end up noticeably larger or
   smaller than the other four -- `pct_min`/`pct_max`/`n` are carried on
   every bucket, bucket 5 included, with no special-casing, so this is
   visible in the rendered report exactly like the other four, not
   inferred or hidden.

Reads `london_range`/`pre_london_range_pct`/`atr_bars` through
`jarvis.features.compute` -- the sanctioned, already-verified path
(D-073) -- and reuses the anchored-extraction pattern D-073 required
(never inferring a day's own value from a null/non-null transition).
Pure computation over an already-loaded bars frame, like `describe.r1`
and `describe.r5`: never reads `data/` itself.
"""

from dataclasses import dataclass

import numpy as np
import polars as pl

from jarvis.core.bootstrap import BootstrapCI, bootstrap_ci
from jarvis.core.types import Nanos
from jarvis.features import compute as compute_features
from jarvis.features.base import session_window_bounds, trading_day_boundaries
from jarvis.sessions import SessionSet

N_BUCKETS = 5

_MIN_BOOTSTRAP_N = 10  # G.1.3's n<10 suppression makes a CI below this unreportable anyway.
_BOOTSTRAP_SEED = 20260914
_N_RESAMPLES = 2000  # same reasoning as R1 (D-074): trading-day granularity, not R5's
# per-bar granularity, so R5's D-068b memory/time pressure does not apply here either.


@dataclass(frozen=True, slots=True)
class BucketStats:
    bucket: int  # 0 (lowest pre_london_range_pct -- most compressed) .. 4 (highest)
    n: int
    pct_min: float | None  # this bucket's own realized pre_london_range_pct range --
    pct_max: float | None  # None only when n == 0.
    median_ratio: BootstrapCI | None  # None if n < _MIN_BOOTSTRAP_N
    q1_ratio: float | None
    q3_ratio: float | None


@dataclass(frozen=True, slots=True)
class YearBuckets:
    year: int
    buckets: tuple[BucketStats, ...]  # length N_BUCKETS, ordered 0..4


@dataclass(frozen=True, slots=True)
class R2Result:
    start_ns: Nanos
    end_ns: Nanos
    bars_examined: int
    pooled: tuple[BucketStats, ...]  # length N_BUCKETS, ordered 0..4 (empty if no eligible days)
    by_year: tuple[YearBuckets, ...]


def _extract_day_values(
    bars: pl.DataFrame,
    days: list,
    day_idx: np.ndarray,
    series_list: list[pl.Series],
    starts: np.ndarray,
    ends: np.ndarray,
) -> list[np.ndarray]:
    """One value per trading day per input series, anchored directly on
    that day's own independently-computed window-close instant for the
    session `starts`/`ends` describe -- never inferred from a null/non-
    null transition in a (D-073-masked) series. NaN for a day with no
    bars in its own window at all, or whose close falls after the last
    bar in this frame. Generalizes `describe.r1`'s own
    `_extract_day_values` to an arbitrary number of series sharing one
    session anchor, rather than importing that module-private helper
    across a report boundary."""
    ts = bars["ts_utc_ns"].to_numpy()
    n_days = len(days)

    in_own_window = (ts >= starts[day_idx]) & (ts < ends[day_idx])
    day_has_own_bars = np.zeros(n_days, dtype=bool)
    day_has_own_bars[day_idx[in_own_window]] = True

    close_bar_idx = np.searchsorted(ts, ends, side="left")
    arrs = [s.to_numpy() for s in series_list]
    outs = [np.full(n_days, np.nan, dtype=np.float64) for _ in series_list]
    for d in range(n_days):
        if not day_has_own_bars[d]:
            continue
        idx = close_bar_idx[d]
        if idx >= len(ts):
            continue
        for out, arr in zip(outs, arrs):
            out[d] = arr[idx]
    return outs


def _bucket_quintiles(pct_values: np.ndarray) -> np.ndarray:
    """Assigns each value in `pct_values` (already filtered to eligible
    days only) a bucket id 0..N_BUCKETS-1, in ascending value order,
    never splitting a run of identical values across two buckets. Buckets
    0..N_BUCKETS-2 close once their running count reaches
    len(pct_values) / N_BUCKETS, at the boundary of the value-group that
    crosses it (that whole group stays in the closing bucket). The final
    bucket absorbs everything left after the second-to-last bucket
    closes -- it is not given the same ~n/5 target, and can end up
    noticeably larger or smaller than the others; see this module's
    docstring."""
    n = pct_values.size
    bucket_ids = np.empty(n, dtype=np.int64)
    if n == 0:
        return bucket_ids

    order = np.argsort(pct_values, kind="stable")
    sorted_vals = pct_values[order]
    target = n / N_BUCKETS

    bucket_of_sorted = np.empty(n, dtype=np.int64)
    bucket = 0
    count_in_bucket = 0
    i = 0
    while i < n:
        j = i
        while j < n and sorted_vals[j] == sorted_vals[i]:
            j += 1
        if bucket < N_BUCKETS - 1 and count_in_bucket > 0 and count_in_bucket >= target:
            bucket += 1
            count_in_bucket = 0
        bucket_of_sorted[i:j] = bucket
        count_in_bucket += j - i
        i = j

    bucket_ids[order] = bucket_of_sorted
    return bucket_ids


def _bootstrap_or_none(values: np.ndarray, *, seed: int) -> BootstrapCI | None:
    if values.size < _MIN_BOOTSTRAP_N:
        return None
    return bootstrap_ci(values, seed=seed, n_resamples=_N_RESAMPLES)


def _bucket_stats(bucket: int, pct_vals: np.ndarray, ratio_vals: np.ndarray, *, seed: int) -> BucketStats:
    n = ratio_vals.size
    if n == 0:
        return BucketStats(bucket=bucket, n=0, pct_min=None, pct_max=None, median_ratio=None, q1_ratio=None, q3_ratio=None)
    q1, q3 = (float(v) for v in np.percentile(ratio_vals, [25, 75]))
    return BucketStats(
        bucket=bucket,
        n=n,
        pct_min=float(np.min(pct_vals)),
        pct_max=float(np.max(pct_vals)),
        median_ratio=_bootstrap_or_none(ratio_vals, seed=seed),
        q1_ratio=q1,
        q3_ratio=q3,
    )


def compute_r2(bars: pl.DataFrame, session_set: SessionSet, *, start_ns: Nanos, end_ns: Nanos) -> R2Result:
    """Compute R2 over `bars`, which the caller must have already loaded
    for exactly `[start_ns, end_ns)` (see `describe.periods.
    stage2_descriptive_range`) -- this function does not re-filter by
    range itself, the same trust-the-caller shape as `describe.r1.
    compute_r1` and `describe.r5.compute_r5` (D-071 names this pattern)."""
    empty = R2Result(start_ns=start_ns, end_ns=end_ns, bars_examined=bars.height, pooled=(), by_year=())
    if bars.height == 0:
        return empty

    days, day_idx = trading_day_boundaries(bars)
    if not days:
        return empty

    year_arr = np.array([d.year for d in days], dtype=np.int64)

    result = compute_features(["london_range", "atr_bars", "pre_london_range_pct"], bars, session_set)

    london_starts, london_ends = session_window_bounds(session_set, "london", days)
    day_range, day_atr = _extract_day_values(
        bars, days, day_idx, [result.frame["london_range"], result.frame["atr_bars"]], london_starts, london_ends
    )

    pre_london_starts, pre_london_ends = session_window_bounds(session_set, "pre_london", days)
    (day_pct,) = _extract_day_values(
        bars, days, day_idx, [result.frame["pre_london_range_pct"]], pre_london_starts, pre_london_ends
    )

    # day_atr is NaN wherever the day is ineligible or ATR's own warmup
    # hasn't completed (numpy: NaN/x and x/NaN are both NaN). An exact
    # day_atr == 0.0 has never been observed against the real dataset
    # (checked directly, overnight audit 2026-09-15) but is not provably
    # impossible, and numpy gives +-inf for nonzero/0.0, which isnan alone
    # does not catch -- np.isfinite excludes NaN and +-inf together, same
    # reasoning and fix as R1's day_atr_ratio. Suppressed rather than left
    # to warn -- the division-by-zero/NaN case is anticipated and handled
    # by the isfinite mask below, not an actual error.
    with np.errstate(divide="ignore", invalid="ignore"):
        day_ratio = day_range / day_atr
    valid = np.isfinite(day_ratio) & ~np.isnan(day_pct)

    eligible_indices = np.nonzero(valid)[0]
    if eligible_indices.size == 0:
        return empty

    pct_valid = day_pct[eligible_indices]
    ratio_valid = day_ratio[eligible_indices]
    year_valid = year_arr[eligible_indices]

    bucket_ids = _bucket_quintiles(pct_valid)

    pooled: list[BucketStats] = []
    for b in range(N_BUCKETS):
        mask = bucket_ids == b
        seed = _BOOTSTRAP_SEED + b * 1_000
        pooled.append(_bucket_stats(b, pct_valid[mask], ratio_valid[mask], seed=seed))

    by_year: list[YearBuckets] = []
    for year in sorted(set(int(y) for y in year_valid)):
        year_mask = year_valid == year
        year_buckets = []
        for b in range(N_BUCKETS):
            mask = year_mask & (bucket_ids == b)
            # Distinct offset block from pooled's own seeds (b*1_000 tops out
            # under 5_000) so pooled and per-year draws never reuse a stream.
            seed = _BOOTSTRAP_SEED + 100_000 + b * 1_000 + (year - 2000)
            year_buckets.append(_bucket_stats(b, pct_valid[mask], ratio_valid[mask], seed=seed))
        by_year.append(YearBuckets(year=year, buckets=tuple(year_buckets)))

    return R2Result(
        start_ns=start_ns,
        end_ns=end_ns,
        bars_examined=bars.height,
        pooled=tuple(pooled),
        by_year=tuple(by_year),
    )
