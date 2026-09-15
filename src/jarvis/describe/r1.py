"""R1 -- Session range anatomy (Technical Bible Part 2 §G.1.2).

Question: how large are the `pre_london`, `london` and `new_york` ranges,
and how has that changed?
Calculation: distribution of each session's range in price and in ATR
units; by year; by day of week (two separate marginal breakdowns per
session, matching R5's own "by hour-of-week; by year" precedent -- not a
joint year-x-weekday cross-tab, which the text does not ask for).
Uncertainty: median with bootstrap 95% CI; interquartile range; n per
cell.
Output: table + box plot by year (the box plot is scoped to the year
axis only, per the exact text -- not also by day of week).

Reads `pre_london_range`/`london_range`/`new_york_range` and `atr_bars`
through `jarvis.features.compute` -- the sanctioned, already-verified,
already-leakage-safe path (D-073), not a reimplementation of session-
range logic here. Pure computation over an already-loaded bars frame,
like `describe.r5`: never reads `data/` itself.
"""

from dataclasses import dataclass

import numpy as np
import polars as pl

from jarvis.core.bootstrap import BootstrapCI, bootstrap_ci
from jarvis.core.types import Nanos
from jarvis.features import compute as compute_features
from jarvis.features.base import session_window_bounds, trading_day_boundaries
from jarvis.sessions import SessionSet

SESSIONS: tuple[str, ...] = ("pre_london", "london", "new_york")

_MIN_BOOTSTRAP_N = 10  # G.1.3's n<10 suppression makes a CI below this unreportable anyway.
_BOOTSTRAP_SEED = 20260914
_N_RESAMPLES = 2000  # core.bootstrap's own default: R1's cells are trading-day-granularity
# (hundreds per cell, not R5's hundreds-of-thousands-of-bars-per-cell), so the
# time/memory pressure that justified R5's 500 (D-068b) does not apply here --
# confirmed by direct benchmark (see WP-020's real run) before committing to this.


@dataclass(frozen=True, slots=True)
class RangeStats:
    n: int
    median_price: BootstrapCI | None  # None if n < _MIN_BOOTSTRAP_N
    median_atr: BootstrapCI | None  # None if n < _MIN_BOOTSTRAP_N
    q1_price: float | None
    q3_price: float | None
    q1_atr: float | None
    q3_atr: float | None


@dataclass(frozen=True, slots=True)
class YearCell:
    year: int
    stats: RangeStats
    raw_atr_values: tuple[float, ...]  # for the box plot's own five-number summary


@dataclass(frozen=True, slots=True)
class WeekdayCell:
    weekday: int  # 0=Monday .. 4=Friday (Python date.weekday()) -- trading days are never Sat/Sun
    stats: RangeStats


@dataclass(frozen=True, slots=True)
class SessionResult:
    session: str
    by_year: tuple[YearCell, ...]
    by_weekday: tuple[WeekdayCell, ...]


@dataclass(frozen=True, slots=True)
class R1Result:
    start_ns: Nanos
    end_ns: Nanos
    bars_examined: int
    sessions: tuple[SessionResult, ...]  # one per SESSIONS entry, same order


def _extract_day_values(
    bars: pl.DataFrame,
    days: list,
    day_idx: np.ndarray,
    range_series: pl.Series,
    atr_series: pl.Series,
    starts: np.ndarray,
    ends: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """One (range, atr-at-close) pair per trading day, anchored directly
    on that day's own independently-computed window-close instant --
    never inferred from a null/non-null transition in the (D-073-masked)
    series (WP-020's own required pattern, mirroring the fix already
    applied to `pre_london_range_pct_compute`). NaN for a day with no
    bars in its own window at all (not eligible), or whose close falls
    after the last bar in this frame (not yet observable)."""
    ts = bars["ts_utc_ns"].to_numpy()
    range_arr = range_series.to_numpy()
    atr_arr = atr_series.to_numpy()
    n_days = len(days)

    in_own_window = (ts >= starts[day_idx]) & (ts < ends[day_idx])
    day_has_own_bars = np.zeros(n_days, dtype=bool)
    day_has_own_bars[day_idx[in_own_window]] = True

    close_bar_idx = np.searchsorted(ts, ends, side="left")
    day_range = np.full(n_days, np.nan, dtype=np.float64)
    day_atr = np.full(n_days, np.nan, dtype=np.float64)
    for d in range(n_days):
        if not day_has_own_bars[d]:
            continue
        idx = close_bar_idx[d]
        if idx >= len(ts):
            continue
        day_range[d] = range_arr[idx]
        day_atr[d] = atr_arr[idx]
    return day_range, day_atr


def _bootstrap_or_none(values: np.ndarray, *, seed: int) -> BootstrapCI | None:
    if values.size < _MIN_BOOTSTRAP_N:
        return None
    return bootstrap_ci(values, seed=seed, n_resamples=_N_RESAMPLES)


def _stats_for(price_values: np.ndarray, atr_values: np.ndarray, *, seed: int) -> RangeStats:
    n = price_values.size
    if n == 0:
        return RangeStats(n=0, median_price=None, median_atr=None, q1_price=None, q3_price=None, q1_atr=None, q3_atr=None)
    q1_price, q3_price = (float(v) for v in np.percentile(price_values, [25, 75]))
    q1_atr, q3_atr = (float(v) for v in np.percentile(atr_values, [25, 75]))
    return RangeStats(
        n=n,
        median_price=_bootstrap_or_none(price_values, seed=seed),
        median_atr=_bootstrap_or_none(atr_values, seed=seed + 1),
        q1_price=q1_price,
        q3_price=q3_price,
        q1_atr=q1_atr,
        q3_atr=q3_atr,
    )


def _compute_session(
    session: str,
    bars: pl.DataFrame,
    session_set: SessionSet,
    days: list,
    day_idx: np.ndarray,
    year_arr: np.ndarray,
    weekday_arr: np.ndarray,
) -> SessionResult:
    range_name = f"{session}_range"
    result = compute_features([range_name, "atr_bars"], bars, session_set)
    range_series = result.frame[range_name]
    atr_series = result.frame["atr_bars"]

    starts, ends = session_window_bounds(session_set, session, days)
    day_range, day_atr = _extract_day_values(bars, days, day_idx, range_series, atr_series, starts, ends)
    # ATR-unit range. day_atr is NaN wherever the day is ineligible or ATR's
    # own warmup hasn't completed (numpy: NaN/x and x/NaN are both NaN,
    # caught by isnan). An exact day_atr == 0.0 has never been observed
    # against the real dataset (checked directly, not assumed -- overnight
    # audit, 2026-09-15) but is not provably impossible, and numpy gives
    # +-inf for a nonzero/0.0 division, which isnan alone does NOT catch --
    # np.isfinite excludes NaN and +-inf together, so a zero-ATR day is
    # excluded the same way a NaN one already is, rather than silently
    # surviving as an infinite ratio. Division by exactly 0.0 (or 0.0/0.0)
    # is anticipated and handled by the isfinite mask below, not an actual
    # error -- suppressed here rather than left to print a numpy warning
    # on every such day.
    with np.errstate(divide="ignore", invalid="ignore"):
        day_atr_ratio = day_range / day_atr

    valid = ~np.isnan(day_range) & np.isfinite(day_atr_ratio)

    # Deterministic per-session offset for seed derivation -- NOT Python's
    # built-in hash() on a string, which is randomised per-process by
    # default (PYTHONHASHSEED) and would silently break this report's
    # reproducibility (Part 2 §F.1). SESSIONS.index(...) is fixed and
    # stable across runs, matching R5's own "derive from the bucket's own
    # key, not a shared constant" reasoning (D-068).
    session_offset = SESSIONS.index(session) * 100_000

    by_year: list[YearCell] = []
    for year in sorted(set(int(y) for y in year_arr[valid])):
        mask = valid & (year_arr == year)
        price_vals = day_range[mask]
        atr_vals = day_atr_ratio[mask]
        seed = _BOOTSTRAP_SEED + session_offset + year
        by_year.append(
            YearCell(
                year=year,
                stats=_stats_for(price_vals, atr_vals, seed=seed),
                raw_atr_values=tuple(float(v) for v in atr_vals),
            )
        )

    by_weekday: list[WeekdayCell] = []
    for weekday in sorted(set(int(w) for w in weekday_arr[valid])):
        mask = valid & (weekday_arr == weekday)
        price_vals = day_range[mask]
        atr_vals = day_atr_ratio[mask]
        seed = _BOOTSTRAP_SEED + session_offset + 50_000 + weekday
        by_weekday.append(WeekdayCell(weekday=weekday, stats=_stats_for(price_vals, atr_vals, seed=seed)))

    return SessionResult(session=session, by_year=tuple(by_year), by_weekday=tuple(by_weekday))


def compute_r1(bars: pl.DataFrame, session_set: SessionSet, *, start_ns: Nanos, end_ns: Nanos) -> R1Result:
    """Compute R1 over `bars`, which the caller must have already loaded
    for exactly `[start_ns, end_ns)` (see `describe.periods.
    stage2_descriptive_range`) -- this function does not re-filter by
    range itself, the same trust-the-caller shape as `describe.r5.
    compute_r5` (D-071 names this pattern; not resolved here)."""
    if bars.height == 0:
        return R1Result(start_ns=start_ns, end_ns=end_ns, bars_examined=0, sessions=())

    days, day_idx = trading_day_boundaries(bars)
    if not days:
        return R1Result(start_ns=start_ns, end_ns=end_ns, bars_examined=bars.height, sessions=())

    # Per-DAY arrays (length len(days)), aligned with day_range/day_atr's
    # own per-day indexing -- NOT per-bar (day_idx is only used inside
    # _extract_day_values, to build those per-day arrays in the first
    # place).
    year_arr = np.array([d.year for d in days], dtype=np.int64)
    weekday_arr = np.array([d.weekday() for d in days], dtype=np.int64)

    sessions = tuple(
        _compute_session(session, bars, session_set, days, day_idx, year_arr, weekday_arr)
        for session in SESSIONS
    )

    return R1Result(start_ns=start_ns, end_ns=end_ns, bars_examined=bars.height, sessions=sessions)
