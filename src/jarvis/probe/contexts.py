"""Stage 0 candidate context detection: C-A/B/C/D (Technical Bible Part
2 SS G.0.3). A pure function of bars, already-computed features, a
SessionSet, and ProbeParams -- it only ever asks "did this pattern
occur," never "was it profitable." This module (and the whole `probe`
package) has no import path to anything that could compute a
profitability proxy, enforced mechanically by the "probe must not
compute profitability" .importlinter contract, not merely by
convention.

`features` is the `.frame` of a `jarvis.features.compute()` result
(ts_utc_ns plus one column per requested feature) -- this module never
calls compute() itself and never bypasses it (WP-007's leakage harness
exists precisely so every feature value handed to `probe` has already
been proven not to read ahead).

EXPLORATORY -- FREQUENCY ONLY -- NOT EVIDENCE OF PREDICTIVE VALUE.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Literal

import numpy as np
import polars as pl

from jarvis.core.types import Nanos
from jarvis.features.base import session_window_bounds, trading_day_boundaries
from jarvis.sessions import SessionSet

ContextId = Literal["C-A", "C-B", "C-C", "C-D"]
Direction = Literal["long", "short", "none"]

_REQUIRED_FEATURE_COLUMNS = ("pre_london_high", "pre_london_low", "pre_london_range_pct", "atr_bars")


@dataclass(frozen=True, slots=True)
class ContextEvent:
    context: ContextId
    trading_day: date
    direction: Direction
    ts_utc_ns: Nanos
    detail: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class ProbeParams:
    range_pct_max: float = 0.33
    range_pct_min: float = 0.67
    break_buffer_atr: float = 0.10
    reentry_buffer_atr: float = 0.05
    reentry_window_bars: int = 60
    atr_period_bars: int = 1440


def _day_value(features: pl.DataFrame, day_idx: np.ndarray, n_days: int, column: str) -> np.ndarray:
    """The single (constant-after-close) non-null value of a
    session_terminal feature column for each trading day, or NaN for a
    day with none (mirrors jarvis.features.library.pre_london_range_pct_
    compute's own day-value extraction -- deliberately the same pattern,
    since both are reading a masked session_terminal series and recovering
    the day's terminal value from whichever bars are already unmasked)."""
    tmp = pl.DataFrame({"_day_idx": day_idx, "_v": features[column]})
    agg = tmp.group_by("_day_idx", maintain_order=True).agg(
        pl.col("_v").drop_nulls().first().alias("_value")
    )
    out = np.full(n_days, np.nan, dtype=np.float64)
    if agg.height:
        out[agg["_day_idx"].to_numpy()] = agg["_value"].to_numpy()
    return out


def detect_events(
    bars: pl.DataFrame,
    features: pl.DataFrame,
    session_set: SessionSet,
    params: ProbeParams,
) -> tuple[ContextEvent, ...]:
    """Detect every C-A/B/C/D event in `bars`, one trading day at a time.

    Exclusion: if the session set's exclude_partial flag is set (fx_core
    v1's is), a trading day is skipped ENTIRELY (all four contexts) when
    either the pre_london or london_open_window session's Window.partial
    is True for that day (overlaps the weekend gap) -- the WP's own
    counting rules name only this exclusion (not the Technical Bible's
    additional "thin days," for which no consumable flag exists anywhere
    in the codebase today; see the package's closing notes).

    C-A/C-D are evaluated once per day, timestamped at the pre_london
    window's own end_ns (the instant the value becomes knowable), using
    whichever bar's already-unmasked pre_london_range_pct value first
    reveals the day's terminal figure.

    C-B is evaluated per direction, per day, at the FIRST bar within
    london_open_window where mid crosses the buffered pre-London extreme;
    dedup is automatic since only the first crossing is ever recorded.

    C-C's 60-PRESENT-bar re-entry window is counted by row position from
    the break bar (bars[break_idx+1 .. break_idx+60]), matching atr_bars'
    own "present bars, not clock minutes" convention -- and is
    deliberately NOT clipped at the trading day boundary, since the WP
    specifies "60 present bars," not "60 present bars or end of day."
    Both the break threshold and the re-entry threshold are denominated
    in the SAME atr_bars value, read once at the break bar -- a single,
    fixed ATR scale for the whole round trip, not a second reading of ATR
    at the (possibly much later) re-entry bar."""
    for col in _REQUIRED_FEATURE_COLUMNS:
        if col not in features.columns:
            raise ValueError(f"detect_events: features frame is missing required column {col!r}")

    if bars.height == 0:
        return ()

    days, day_idx = trading_day_boundaries(bars)
    if not days:
        return ()
    n_days = len(days)

    pre_starts, pre_ends = session_window_bounds(session_set, "pre_london", days)
    lo_starts, lo_ends = session_window_bounds(session_set, "london_open_window", days)

    exclude_partial = session_set.definition.exclude_partial
    day_excluded = np.zeros(n_days, dtype=bool)
    if exclude_partial:
        for i, d in enumerate(days):
            if (
                session_set.window("pre_london", d).partial
                or session_set.window("london_open_window", d).partial
            ):
                day_excluded[i] = True

    ts = bars["ts_utc_ns"].to_numpy()
    mid = ((bars["bid_c"] + bars["ask_c"]) / 2.0).to_numpy()
    atr = features["atr_bars"].to_numpy().astype(np.float64)

    day_range_pct = _day_value(features, day_idx, n_days, "pre_london_range_pct")
    day_pre_high = _day_value(features, day_idx, n_days, "pre_london_high")
    day_pre_low = _day_value(features, day_idx, n_days, "pre_london_low")

    events: list[ContextEvent] = []

    for i, d in enumerate(days):
        if day_excluded[i]:
            continue

        rp = day_range_pct[i]
        if not np.isnan(rp):
            if rp <= params.range_pct_max:
                events.append(
                    ContextEvent(
                        context="C-A",
                        trading_day=d,
                        direction="none",
                        ts_utc_ns=Nanos(int(pre_ends[i])),
                        detail={"pre_london_range_pct": float(rp)},
                    )
                )
            if rp >= params.range_pct_min:
                events.append(
                    ContextEvent(
                        context="C-D",
                        trading_day=d,
                        direction="none",
                        ts_utc_ns=Nanos(int(pre_ends[i])),
                        detail={"pre_london_range_pct": float(rp)},
                    )
                )

        high = day_pre_high[i]
        low = day_pre_low[i]
        if np.isnan(high) or np.isnan(low):
            continue  # no pre-London range this day -> C-B/C-C undefined

        window_mask = (day_idx == i) & (ts >= lo_starts[i]) & (ts < lo_ends[i])
        window_idxs = np.nonzero(window_mask)[0]
        if len(window_idxs) == 0:
            continue

        for direction in ("long", "short"):
            break_idx = None
            for k in window_idxs:
                if np.isnan(atr[k]):
                    continue
                if direction == "long":
                    triggered = mid[k] > high + params.break_buffer_atr * atr[k]
                else:
                    triggered = mid[k] < low - params.break_buffer_atr * atr[k]
                if triggered:
                    break_idx = int(k)
                    break
            if break_idx is None:
                continue

            break_ts = Nanos(int(ts[break_idx]))
            break_atr = atr[break_idx]
            events.append(
                ContextEvent(
                    context="C-B",
                    trading_day=d,
                    direction=direction,
                    ts_utc_ns=break_ts,
                    detail={"mid": float(mid[break_idx]), "atr_bars": float(break_atr)},
                )
            )

            reentry_lo = break_idx + 1
            reentry_hi = min(break_idx + params.reentry_window_bars, bars.height - 1)
            reentered = False
            for k in range(reentry_lo, reentry_hi + 1):
                if direction == "long":
                    if mid[k] <= high - params.reentry_buffer_atr * break_atr:
                        reentered = True
                        break
                else:
                    if mid[k] >= low + params.reentry_buffer_atr * break_atr:
                        reentered = True
                        break
            if reentered:
                events.append(
                    ContextEvent(
                        context="C-C",
                        trading_day=d,
                        direction=direction,
                        ts_utc_ns=break_ts,  # timestamped at the C-B trigger, per spec
                        detail={"break_atr_bars": float(break_atr)},
                    )
                )

    return tuple(events)


def context_eligible_days(
    bars: pl.DataFrame, features: pl.DataFrame, session_set: SessionSet
) -> frozenset[date]:
    """Every trading day for which every feature the four contexts need
    (pre_london_high, pre_london_low, pre_london_range_pct, and at least
    one non-null atr_bars reading within the day) is available -- feature
    AVAILABILITY, a distinct concept from a year's data-completeness
    admissibility (hours present, zero QA errors), which this function
    knows nothing about and does not compute. Partial-session exclusion
    is NOT applied here: a day can be feature-eligible while still being
    excluded from context counting for being a partial session, and
    conflating the two would hide exactly the D-036-warmup-worked-or-not
    signal this column exists to surface."""
    if bars.height == 0:
        return frozenset()

    days, day_idx = trading_day_boundaries(bars)
    if not days:
        return frozenset()
    n_days = len(days)

    for col in _REQUIRED_FEATURE_COLUMNS:
        if col not in features.columns:
            raise ValueError(f"context_eligible_days: features frame is missing column {col!r}")

    day_range_pct = _day_value(features, day_idx, n_days, "pre_london_range_pct")
    day_pre_high = _day_value(features, day_idx, n_days, "pre_london_high")
    day_pre_low = _day_value(features, day_idx, n_days, "pre_london_low")

    atr = features["atr_bars"].to_numpy().astype(np.float64)
    atr_present = np.zeros(n_days, dtype=bool)
    non_null_atr_days = day_idx[~np.isnan(atr)]
    if len(non_null_atr_days):
        atr_present[np.unique(non_null_atr_days)] = True

    eligible = (
        ~np.isnan(day_range_pct) & ~np.isnan(day_pre_high) & ~np.isnan(day_pre_low) & atr_present
    )
    return frozenset(d for i, d in enumerate(days) if eligible[i])
