"""The Stage 0 feature library: atr_bars, pre_london_{high,low,range},
pre_london_range_pct, rv_60m. Six registry entries implementing the five
features named in WP-007 (pre_london_high/low/range are grouped as one
feature concept in the WP's framing but are three separate FeatureDefs,
since each has its own name and its own null/leakage behaviour to
verify independently).

Every session_terminal feature here returns the EVENTUAL per-trading-day
value broadcast to every bar of that day -- visibility (nulling before
the session window closes) is NOT this module's job. It is enforced
uniformly by jarvis.features.compute's orchestration via
base.apply_session_terminal_mask, using each FeatureDef's own
params["session"]. This means a bug in one feature's own arithmetic
cannot accidentally leak data past the window close: the masking is
applied identically to every session_terminal feature regardless of what
its compute() function returns.
"""

import numpy as np
import polars as pl

from jarvis.features.base import (
    FeatureContext,
    FeatureDef,
    LookbackSpec,
    register,
    session_window_bounds,
    trading_day_boundaries,
)

NS_PER_MINUTE = 60_000_000_000


def mid_prices(bars: pl.DataFrame) -> np.ndarray:
    return ((bars["bid_c"] + bars["ask_c"]) / 2).to_numpy()


# ---------------------------------------------------------------------------
# atr_bars (causal)
# ---------------------------------------------------------------------------


def atr_bars_compute(ctx: FeatureContext) -> pl.Series:
    """Wilder ATR over the trailing n PRESENT bars (row position, never
    clock time), true range on the bid series.

    Indexing, resolved against an apparent tension in the spec: "null for
    the first n bars" read literally (positions 0..n-1, n positions null)
    would require the Wilder recursion to skip the bar-n TR value the
    moment the seed uses TR[0..n-1] -- discarding real data, not merely
    an indexing convention. The mathematically necessary version seeds
    ATR[n-1] with mean(TR[0..n-1]) (using a FULL n-TR window, so "not a
    partial estimate" holds exactly) and continues the recursion from
    TR[n] onward with nothing skipped -- so exactly n-1 bars (0..n-2) are
    null and bar index n-1 (the nth bar) is the first non-null value.
    This is also the standard Wilder/RSI-family convention. It is
    consistent with the leakage harness's own L-4 requirement ("with
    lookback.n - 1 bars of history, output entirely null"): n-1 bars of
    history is one short of the n needed for the seed, so everything is
    null, matching this implementation exactly.
    """
    n = int(ctx.params["n"])
    bars = ctx.bars
    m = bars.height

    bid_h = bars["bid_h"].to_numpy()
    bid_l = bars["bid_l"].to_numpy()
    bid_c = bars["bid_c"].to_numpy()

    tr = np.empty(m, dtype=np.float64)
    if m > 0:
        tr[0] = bid_h[0] - bid_l[0]  # first bar: no previous close
    if m > 1:
        prev_close = bid_c[:-1]
        hl = bid_h[1:] - bid_l[1:]
        hc = np.abs(bid_h[1:] - prev_close)
        lc = np.abs(bid_l[1:] - prev_close)
        tr[1:] = np.maximum(hl, np.maximum(hc, lc))

    atr = np.full(m, np.nan, dtype=np.float64)
    if m >= n and n > 0:
        seed = float(np.mean(tr[:n]))
        atr[n - 1] = seed
        prev = seed
        for i in range(n, m):
            prev = ((n - 1) * prev + tr[i]) / n
            atr[i] = prev

    return pl.Series("atr_bars", atr).fill_nan(None)


register(
    FeatureDef(
        name="atr_bars",
        version=1,
        dtype=pl.Float64,
        lookback=LookbackSpec("bars", 1440),
        gap_tolerance_ns=None,
        requires=(),
        params={"n": 1440},
        leakage_class="causal",
        compute=atr_bars_compute,
    )
)


# ---------------------------------------------------------------------------
# pre_london_high / pre_london_low (session_terminal)
# ---------------------------------------------------------------------------


def _pre_london_extreme_compute(ctx: FeatureContext, *, which: str) -> pl.Series:
    """The eventual high or low of `mid` over the named session's window,
    per trading day, broadcast to every bar of that day. A trading day
    with no bars inside the window (market closed, or a hole) gets null
    for the whole day -- never a partial-window estimate."""
    bars = ctx.bars
    session_name = str(ctx.params["session"])
    name = "pre_london_high" if which == "high" else "pre_london_low"

    days, day_idx = trading_day_boundaries(bars)
    if not days:
        return pl.Series(name, [], dtype=pl.Float64)

    window_starts, window_ends = session_window_bounds(ctx.session_set, session_name, days)

    ts = bars["ts_utc_ns"].to_numpy()
    mid = mid_prices(bars)

    bar_window_start = window_starts[day_idx]
    bar_window_end = window_ends[day_idx]
    in_window = (ts >= bar_window_start) & (ts < bar_window_end)

    n_days = len(days)
    masked_day_idx = day_idx[in_window]
    masked_mid = mid[in_window]

    if which == "high":
        day_values = np.full(n_days, -np.inf, dtype=np.float64)
        if len(masked_day_idx):
            np.maximum.at(day_values, masked_day_idx, masked_mid)
        day_values[np.isneginf(day_values)] = np.nan
    else:
        day_values = np.full(n_days, np.inf, dtype=np.float64)
        if len(masked_day_idx):
            np.minimum.at(day_values, masked_day_idx, masked_mid)
        day_values[np.isposinf(day_values)] = np.nan

    per_bar = day_values[day_idx]
    return pl.Series(name, per_bar).fill_nan(None)


def pre_london_high_compute(ctx: FeatureContext) -> pl.Series:
    return _pre_london_extreme_compute(ctx, which="high")


def pre_london_low_compute(ctx: FeatureContext) -> pl.Series:
    return _pre_london_extreme_compute(ctx, which="low")


register(
    FeatureDef(
        name="pre_london_high",
        version=1,
        dtype=pl.Float64,
        lookback=LookbackSpec("sessions", 1),
        gap_tolerance_ns=None,
        requires=(),
        params={"session": "pre_london"},
        leakage_class="session_terminal",
        compute=pre_london_high_compute,
    )
)

register(
    FeatureDef(
        name="pre_london_low",
        version=1,
        dtype=pl.Float64,
        lookback=LookbackSpec("sessions", 1),
        gap_tolerance_ns=None,
        requires=(),
        params={"session": "pre_london"},
        leakage_class="session_terminal",
        compute=pre_london_low_compute,
    )
)


# ---------------------------------------------------------------------------
# pre_london_range (session_terminal)
# ---------------------------------------------------------------------------


def pre_london_range_compute(ctx: FeatureContext) -> pl.Series:
    high = ctx.computed["pre_london_high"]
    low = ctx.computed["pre_london_low"]
    return (high - low).alias("pre_london_range")


register(
    FeatureDef(
        name="pre_london_range",
        version=1,
        dtype=pl.Float64,
        lookback=LookbackSpec("sessions", 1),
        gap_tolerance_ns=None,
        requires=("pre_london_high", "pre_london_low"),
        params={"session": "pre_london"},
        leakage_class="session_terminal",
        compute=pre_london_range_compute,
    )
)


# ---------------------------------------------------------------------------
# pre_london_range_pct (session_terminal)
# ---------------------------------------------------------------------------


def pre_london_range_pct_compute(ctx: FeatureContext) -> pl.Series:
    """Percentile rank of today's pre_london_range within the trailing n
    (default 60) PRIOR trading days that themselves have a non-null
    range -- a day with no bars in the window (hence null range) does not
    occupy a slot in the trailing window; it is skipped entirely, exactly
    like W-05's treatment of weekend labels in WP-006. Requires exactly n
    prior ELIGIBLE days; fewer -> null (this is D-036's 60-day warmup).

    Ties: strictly-less-than in the numerator, so a day exactly tying the
    current range does not count as "exceeded" -- ties are conservative,
    not treated as evidence of a new extreme.

    ctx.computed["pre_london_range"] arrives already session_terminal-
    masked (null before window close, constant for the rest of the day).
    Reading "today's own value" from a masked series is still correct:
    once window close has passed for a given day, that day's value is a
    single non-null constant for the remainder of the day, so
    drop_nulls().first() per day recovers it regardless of the mask.
    """
    bars = ctx.bars
    n = int(ctx.params["n"])
    range_series = ctx.computed["pre_london_range"]

    days, day_idx = trading_day_boundaries(bars)
    name = "pre_london_range_pct"
    if not days:
        return pl.Series(name, [], dtype=pl.Float64)

    per_day = (
        pl.DataFrame({"_day_idx": day_idx, "_range": range_series})
        .group_by("_day_idx", maintain_order=True)
        .agg(pl.col("_range").drop_nulls().first().alias("_value"))
    )

    n_days = len(days)
    day_value = np.full(n_days, np.nan, dtype=np.float64)
    idx_present = per_day["_day_idx"].to_numpy()
    vals_present = per_day["_value"].to_numpy()
    day_value[idx_present] = vals_present

    eligible_mask = ~np.isnan(day_value)
    eligible_day_indices = np.nonzero(eligible_mask)[0]
    eligible_values = day_value[eligible_day_indices]

    pct_by_day_idx = np.full(n_days, np.nan, dtype=np.float64)
    for j in range(len(eligible_day_indices)):
        if j < n:
            continue
        prior = eligible_values[j - n : j]
        today_val = eligible_values[j]
        pct = float(np.sum(prior < today_val)) / n
        pct_by_day_idx[eligible_day_indices[j]] = pct

    per_bar = pct_by_day_idx[day_idx]
    return pl.Series(name, per_bar).fill_nan(None)


register(
    FeatureDef(
        name="pre_london_range_pct",
        version=1,
        dtype=pl.Float64,
        lookback=LookbackSpec("trading_days", 60),
        gap_tolerance_ns=None,
        requires=("pre_london_range",),
        params={"session": "pre_london", "n": 60},
        leakage_class="session_terminal",
        compute=pre_london_range_pct_compute,
    )
)


# ---------------------------------------------------------------------------
# rv_60m (causal)
# ---------------------------------------------------------------------------


def rv_60m_compute(ctx: FeatureContext) -> pl.Series:
    """sqrt(sum(ret_1m^2)) over the trailing n=60 present bars, no
    annualisation. ret_1m_i = ln(mid_i/mid_{i-1}), undefined at bar 0 (no
    prior bar) -- so a full 60-return window needs 61 price bars, and the
    first valid output is at bar index n=60 (0-indexed), matching "null
    for the first 60 bars" literally (indices 0..59 null).

    Null if any bar in the 60-bar window has prev_gap_ns exceeding
    gap_tolerance_ns -- read directly from BAR_SCHEMA's own prev_gap_ns
    column (already computed by the resampler), not re-derived from
    timestamps. Bar 0's prev_gap_ns is itself null (no predecessor); that
    is not a gap violation, just the absence of one, so it is treated as
    0 (no gap) for this check.
    """
    n = int(ctx.params["n"])
    # gap_tolerance_ns lives on FeatureDef, not FeatureContext (the compute
    # interface only threads `params` through) -- compute.py's orchestration
    # derives it into params from FeatureDef.gap_tolerance_ns on every call,
    # so FeatureDef stays the single source of truth (not hand-duplicated
    # here). A caller invoking this function directly, bypassing compute(),
    # must still supply it explicitly, same as any other param.
    gap_tolerance_ns = int(ctx.params["gap_tolerance_ns"])
    bars = ctx.bars
    m = bars.height

    mid = mid_prices(bars)
    prev_gap = bars["prev_gap_ns"].to_numpy().astype(np.float64)  # nan where null

    log_mid = np.log(mid)
    ret = np.empty(m, dtype=np.float64)
    if m > 0:
        ret[0] = np.nan
    if m > 1:
        ret[1:] = log_mid[1:] - log_mid[:-1]

    ret_sq = np.nan_to_num(ret * ret, nan=0.0)
    cumsum = np.concatenate(([0.0], np.cumsum(ret_sq)))

    gap_ok = np.isnan(prev_gap) | (prev_gap <= gap_tolerance_ns)
    # cumulative count of gap violations, for O(1) any-violation-in-window checks
    violation = (~gap_ok).astype(np.int64)
    violation_cumsum = np.concatenate(([0], np.cumsum(violation)))

    rv = np.full(m, np.nan, dtype=np.float64)
    for i in range(n, m):  # first valid window ends at bar index n (needs bars i-n..i)
        lo = i - n + 1
        # window = bars [lo, i], i.e. ret_1m values ret[lo..i] (n values,
        # ret[lo] uses price at lo-1 == i-n, so this window's PRICE span
        # is [i-n, i] -- n+1 prices, n returns).
        if violation_cumsum[i + 1] - violation_cumsum[lo] > 0:
            continue
        window_sum = cumsum[i + 1] - cumsum[lo]
        rv[i] = np.sqrt(window_sum)

    return pl.Series("rv_60m", rv).fill_nan(None)


register(
    FeatureDef(
        name="rv_60m",
        version=1,
        dtype=pl.Float64,
        lookback=LookbackSpec("bars", 60),
        gap_tolerance_ns=5 * 60 * 1_000_000_000,
        requires=(),
        params={"n": 60},
        leakage_class="causal",
        compute=rv_60m_compute,
    )
)
