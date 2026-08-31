"""Feature registry: FeatureDef, FeatureContext, dependency resolution,
and the shared day-bucketing/session-window utilities every feature that
touches session boundaries needs.

`features` may import `timeengine` and `sessions` but not `bars` (§B.3:
features receive frames as arguments, they never open Parquet
themselves) -- so any bar-frame-to-trading-day machinery needed here is
necessarily reimplemented against plain numpy/timeengine, not borrowed
from `bars` or `qa` (which cannot be imported either).
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

import numpy as np
import polars as pl

from jarvis.core.errors import UserError
from jarvis.core.types import Nanos
from jarvis.sessions import SessionSet
from jarvis.timeengine import trading_day, trading_day_bounds

Lookback = Literal["bars", "sessions", "trading_days"]
LeakageClass = Literal["causal", "session_terminal"]


@dataclass(frozen=True, slots=True)
class LookbackSpec:
    unit: Lookback
    n: int


@dataclass(frozen=True, slots=True)
class FeatureContext:
    bars: pl.DataFrame  # ascending ts_utc_ns, BAR_SCHEMA columns
    computed: Mapping[str, pl.Series]  # already-computed dependencies
    session_set: SessionSet
    params: Mapping[str, float | int | str]


@dataclass(frozen=True, slots=True)
class FeatureDef:
    name: str
    version: int
    dtype: pl.DataType
    lookback: LookbackSpec
    gap_tolerance_ns: int | None
    requires: tuple[str, ...]  # other feature names
    params: Mapping[str, float | int | str]
    leakage_class: LeakageClass
    compute: Callable[[FeatureContext], pl.Series]


REGISTRY: dict[str, FeatureDef] = {}


def register(defn: FeatureDef) -> None:
    """Raises UserError on duplicate name or unknown dependency.

    core/errors.py is forbidden to change and has no FeatureError, so
    registry/definition-time problems use UserError (bad input, not a
    runtime data-integrity violation) -- see the module docstring in
    compute.py for the IntegrityError side of this split."""
    if defn.name in REGISTRY:
        raise UserError(f"feature {defn.name!r} is already registered")
    for dep in defn.requires:
        if dep not in REGISTRY:
            raise UserError(
                f"feature {defn.name!r} requires unknown dependency {dep!r} "
                "-- dependencies must be registered before the feature that needs them"
            )
    REGISTRY[defn.name] = defn


def resolve_order(names: Sequence[str]) -> tuple[str, ...]:
    """Topologically sort `names` plus their transitive dependencies (each
    dependency appears before every feature that requires it). Raises
    UserError on an unknown feature name or a dependency cycle."""
    order: list[str] = []
    visited: set[str] = set()
    in_progress: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in in_progress:
            raise UserError(f"dependency cycle detected involving feature {name!r}")
        defn = REGISTRY.get(name)
        if defn is None:
            raise UserError(f"unknown feature: {name!r}")
        in_progress.add(name)
        for dep in defn.requires:
            visit(dep)
        in_progress.discard(name)
        visited.add(name)
        order.append(name)

    for name in names:
        visit(name)

    return tuple(order)


# ---------------------------------------------------------------------------
# Shared day-bucketing / session-window utilities
# ---------------------------------------------------------------------------


def trading_day_boundaries(bars: pl.DataFrame) -> tuple[list[date], np.ndarray]:
    """Every trading-day label whose bounds overlap `bars`' ts_utc_ns
    range, plus a per-bar day index into that list.

    Vectorised via np.searchsorted against trading_day_bounds rather than
    calling timeengine.trading_day() once per bar: at 16-year scale
    (~5.9M bars) a per-bar zoneinfo conversion dominates runtime, while
    this needs only one trading_day()/trading_day_bounds() call per
    distinct DAY (a few thousand, not millions)."""
    if bars.height == 0:
        return [], np.array([], dtype=np.int64)

    ts = bars["ts_utc_ns"].to_numpy()
    start_ns = Nanos(int(ts[0]))
    end_ns = Nanos(int(ts[-1]) + 1)

    day = trading_day(start_ns)
    days: list[date] = []
    day_starts: list[int] = []
    while True:
        s, _e = trading_day_bounds(day)
        if s > end_ns:
            break
        days.append(day)
        day_starts.append(int(s))
        day = day + timedelta(days=1)

    day_starts_arr = np.array(day_starts, dtype=np.int64)
    day_idx = np.searchsorted(day_starts_arr, ts, side="right") - 1
    day_idx = np.clip(day_idx, 0, len(days) - 1)
    return days, day_idx


def session_window_bounds(
    session_set: SessionSet, session_name: str, days: list[date]
) -> tuple[np.ndarray, np.ndarray]:
    """(start_ns, end_ns) arrays, one pair per day in `days`, via
    SessionSet.window (already cached per (name, trading_day) inside
    SessionSet itself)."""
    starts = np.empty(len(days), dtype=np.int64)
    ends = np.empty(len(days), dtype=np.int64)
    for i, d in enumerate(days):
        window = session_set.window(session_name, d)
        starts[i] = window.start_ns
        ends[i] = window.end_ns
    return starts, ends


def apply_session_terminal_mask(
    series: pl.Series, bars: pl.DataFrame, session_set: SessionSet, session_name: str
) -> pl.Series:
    """Null out `series` for every bar with ts_utc_ns < that trading day's
    named-session window end. This is the mechanical enforcement behind
    leakage_class == "session_terminal" (Technical Bible Part F §F.2): a
    session_terminal feature's raw compute() may return a value for every
    bar of the day (the eventual terminal value, broadcast), and THIS
    function is what actually makes it invisible before the window
    closes -- independent of whatever the feature's own compute() did or
    didn't do, so a bug in one feature's own masking logic cannot leak
    data (defence in depth, not the only line of defence)."""
    if bars.height == 0:
        return series

    days, day_idx = trading_day_boundaries(bars)
    _starts, ends = session_window_bounds(session_set, session_name, days)
    ts = bars["ts_utc_ns"].to_numpy()
    bar_window_end = ends[day_idx]
    hide = ts < bar_window_end

    # pl.when/then/otherwise builds an Expr, not a Series -- it must be
    # evaluated against a DataFrame (via select) to get a concrete Series
    # back out.
    tmp = pl.DataFrame({"_val": series, "_hide": hide})
    result = tmp.select(
        pl.when(pl.col("_hide")).then(None).otherwise(pl.col("_val")).alias(series.name)
    )
    return result[series.name]
