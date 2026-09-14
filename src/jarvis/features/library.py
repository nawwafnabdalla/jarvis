"""The Stage 0 feature library: atr_bars, pre_london_{high,low,range},
pre_london_range_pct, rv_60m, and (WP-020, for R1) london_{high,low,range}
and new_york_{high,low,range}. pre_london_high/low/range are grouped as
one feature concept in WP-007's framing but are three separate
FeatureDefs, since each has its own name and its own null/leakage
behaviour to verify independently; london/new_york's own high/low/range
triples follow the identical shape, sharing the same underlying
session-generic compute functions parameterized by `params["session"]`
(D-069/WP-020) rather than being copy-pasted per session.

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
# {session}_high / {session}_low (session_terminal) -- session-generic
# since WP-020/D-069: the math was already parameterized by
# params["session"]; only the internal Series label was ever hardcoded to
# pre_london, and compute()'s own `.alias(name)` (features/compute.py)
# overwrites that label unconditionally before it is ever exposed or read
# by anything downstream (apply_session_terminal_mask takes session_name
# as its own explicit argument, never from the Series). So the pre-WP-020
# hardcoding was cosmetic, not a functional defect -- fixed anyway, since
# a Series correctly labelled for the session it was actually computed
# over is simply correct, and leaving it wrong is a landmine for any
# future caller that doesn't go through compute()'s re-aliasing.
# ---------------------------------------------------------------------------


def _session_extreme_compute(ctx: FeatureContext, *, which: str) -> pl.Series:
    """The eventual high or low of `mid` over the named session's window,
    per trading day, broadcast to every bar of that day. A trading day
    with no bars inside the window (market closed, or a hole) gets null
    for the whole day -- never a partial-window estimate."""
    bars = ctx.bars
    session_name = str(ctx.params["session"])
    name = f"{session_name}_{which}"

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
    return _session_extreme_compute(ctx, which="high")


def pre_london_low_compute(ctx: FeatureContext) -> pl.Series:
    return _session_extreme_compute(ctx, which="low")


def london_high_compute(ctx: FeatureContext) -> pl.Series:
    return _session_extreme_compute(ctx, which="high")


def london_low_compute(ctx: FeatureContext) -> pl.Series:
    return _session_extreme_compute(ctx, which="low")


def new_york_high_compute(ctx: FeatureContext) -> pl.Series:
    return _session_extreme_compute(ctx, which="high")


def new_york_low_compute(ctx: FeatureContext) -> pl.Series:
    return _session_extreme_compute(ctx, which="low")


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

register(
    FeatureDef(
        name="london_high",
        version=1,
        dtype=pl.Float64,
        lookback=LookbackSpec("sessions", 1),
        gap_tolerance_ns=None,
        requires=(),
        params={"session": "london"},
        leakage_class="session_terminal",
        compute=london_high_compute,
    )
)

register(
    FeatureDef(
        name="london_low",
        version=1,
        dtype=pl.Float64,
        lookback=LookbackSpec("sessions", 1),
        gap_tolerance_ns=None,
        requires=(),
        params={"session": "london"},
        leakage_class="session_terminal",
        compute=london_low_compute,
    )
)

register(
    FeatureDef(
        name="new_york_high",
        version=1,
        dtype=pl.Float64,
        lookback=LookbackSpec("sessions", 1),
        gap_tolerance_ns=None,
        requires=(),
        params={"session": "new_york"},
        leakage_class="session_terminal",
        compute=new_york_high_compute,
    )
)

register(
    FeatureDef(
        name="new_york_low",
        version=1,
        dtype=pl.Float64,
        lookback=LookbackSpec("sessions", 1),
        gap_tolerance_ns=None,
        requires=(),
        params={"session": "new_york"},
        leakage_class="session_terminal",
        compute=new_york_low_compute,
    )
)


# ---------------------------------------------------------------------------
# {session}_range (session_terminal) -- session-generic since WP-020,
# unlike the extreme functions above, this one previously hardcoded real
# behaviour (the ctx.computed[...] dict-key lookups), not just a cosmetic
# label -- a london_range FeatureDef pointed at the old, unparameterized
# function would have silently read pre_london's high/low. Fixed by
# deriving both the lookup keys and the output name from
# params["session"], which reproduces pre_london_range's exact prior
# behaviour for session="pre_london" (the naming convention already
# matched: "pre_london" + "_high"/"_low"/"_range").
# ---------------------------------------------------------------------------


def _session_range_compute(ctx: FeatureContext) -> pl.Series:
    session_name = str(ctx.params["session"])
    high = ctx.computed[f"{session_name}_high"]
    low = ctx.computed[f"{session_name}_low"]
    return (high - low).alias(f"{session_name}_range")


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
        compute=_session_range_compute,
    )
)

register(
    FeatureDef(
        name="london_range",
        version=1,
        dtype=pl.Float64,
        lookback=LookbackSpec("sessions", 1),
        gap_tolerance_ns=None,
        requires=("london_high", "london_low"),
        params={"session": "london"},
        leakage_class="session_terminal",
        compute=_session_range_compute,
    )
)

register(
    FeatureDef(
        name="new_york_range",
        version=1,
        dtype=pl.Float64,
        lookback=LookbackSpec("sessions", 1),
        gap_tolerance_ns=None,
        requires=("new_york_high", "new_york_low"),
        params={"session": "new_york"},
        leakage_class="session_terminal",
        compute=_session_range_compute,
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

    WP-020/D-073: `ctx.computed["pre_london_range"]` arrives already
    session_terminal-masked under the "most recently completed instance"
    semantic -- a day's EARLY bars can now legitimately show a PRIOR
    day's already-completed value (D-073), not just null. "First non-null
    bar of the day" therefore no longer safely identifies "this day's own
    value" -- it would pick up yesterday's carried-forward value instead.
    Two things are extracted independently rather than inferred from the
    mask's own null/non-null transition: (1) whether day D had any bars
    in its OWN window at all (`day_has_own_bars`, computed directly from
    `bars`' timestamps against that day's own window bounds -- the same
    inputs `_session_extreme_compute` uses, never the masked series) --
    this alone decides eligibility, exactly reproducing the original
    "no bars this day -> skip entirely" rule; (2) for an eligible day,
    its own value is read from the masked series at the first bar AT OR
    AFTER that day's own independently-computed window-close instant
    (`session_window_bounds`) -- by construction the mask has already
    revealed day D's own value there (D-073's "own_day_closed" case), so
    this anchor is correct regardless of what the masking semantic does
    on either side of it.
    """
    bars = ctx.bars
    n = int(ctx.params["n"])
    session_name = str(ctx.params["session"])
    range_series = ctx.computed["pre_london_range"]

    days, day_idx = trading_day_boundaries(bars)
    name = "pre_london_range_pct"
    if not days:
        return pl.Series(name, [], dtype=pl.Float64)

    n_days = len(days)
    starts, ends = session_window_bounds(ctx.session_set, session_name, days)
    ts = bars["ts_utc_ns"].to_numpy()
    range_arr = range_series.to_numpy()

    bar_window_start = starts[day_idx]
    bar_window_end = ends[day_idx]
    in_own_window = (ts >= bar_window_start) & (ts < bar_window_end)
    day_has_own_bars = np.zeros(n_days, dtype=bool)
    day_has_own_bars[day_idx[in_own_window]] = True

    close_bar_idx = np.searchsorted(ts, ends, side="left")
    day_value = np.full(n_days, np.nan, dtype=np.float64)
    for d in range(n_days):
        if not day_has_own_bars[d]:
            continue  # no bars in this day's own window -> not eligible, per the docstring's rule
        idx = close_bar_idx[d]
        if idx >= len(ts):
            continue  # this day's own window closes after the last bar in this frame -- not yet observable
        day_value[d] = range_arr[idx]

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
