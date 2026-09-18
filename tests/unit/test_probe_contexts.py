from datetime import date, datetime, timezone

import polars as pl
import pytest

from jarvis.bars import BAR_SCHEMA
from jarvis.core.types import Nanos
from jarvis.probe.contexts import ProbeParams, context_eligible_days, detect_events
from jarvis.sessions import load_session_set

NS_PER_MINUTE = 60_000_000_000

SESSION_SET = load_session_set("fx_core", 1)


def _ns(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> Nanos:
    return Nanos(int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp()) * 1_000_000_000)


def _bar_row(ts_ns: int, mid: float, prev_gap_ns: int | None = None) -> dict:
    bid_c = mid - 0.0001
    ask_c = mid + 0.0001
    return {
        "ts_utc_ns": ts_ns,
        "bid_o": bid_c,
        "bid_h": bid_c + 0.00005,
        "bid_l": bid_c - 0.00005,
        "bid_c": bid_c,
        "ask_o": ask_c,
        "ask_h": ask_c + 0.00005,
        "ask_l": ask_c - 0.00005,
        "ask_c": ask_c,
        "tick_count": 1,
        "first_tick_ns": ts_ns,
        "last_tick_ns": ts_ns,
        "spread_open": 0.0002,
        "spread_max": 0.0002,
        "spread_twa": 0.0002,
        "prev_gap_ns": prev_gap_ns,
    }


def _bars_frame(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=BAR_SCHEMA)


def _features_frame(
    ts_list: list[int],
    *,
    range_pct: list[float | None],
    pre_high: list[float | None],
    pre_low: list[float | None],
    atr: list[float | None],
) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts_utc_ns": ts_list,
            "pre_london_range_pct": range_pct,
            "pre_london_high": pre_high,
            "pre_london_low": pre_low,
            "atr_bars": atr,
        },
        schema={
            "ts_utc_ns": pl.Int64,
            "pre_london_range_pct": pl.Float64,
            "pre_london_high": pl.Float64,
            "pre_london_low": pl.Float64,
            "atr_bars": pl.Float64,
        },
    )


def _find(events, context: str, direction: str = None):
    matches = [e for e in events if e.context == context and (direction is None or e.direction == direction)]
    return matches


# C-A ------------------------------------------------------------------


def _ca_cd_bars_and_features(range_pct: float):
    """One anchor bar inside pre_london's own window [00:00,08:00) UTC
    (2024-01-16, January -- no DST offset), establishing that day's
    eligibility under the D-083 anchored-extraction fix (a day needs a
    real bar inside the session's own window to be eligible at all,
    matching pre_london_range_pct_compute's identical rule); one value
    bar strictly after close, carrying the actual range_pct the test
    wants to check (the value is read from the first bar AT OR AFTER
    close, a different requirement from eligibility -- a single bar
    can never satisfy both, since one must be < close and the other
    >= close). Both bars are well clear of london_open_window, so only
    C-A/C-D are in play."""
    anchor_ts = _ns(2024, 1, 16, 7, 0)
    value_ts = _ns(2024, 1, 16, 8, 10)
    ts_list = [anchor_ts, value_ts]
    bars = _bars_frame([_bar_row(t, 1.1000) for t in ts_list])
    features = _features_frame(
        ts_list, range_pct=[None, range_pct], pre_high=[None, None], pre_low=[None, None], atr=[None, None]
    )
    return bars, features


def test_context_a_detected_when_range_pct_low():
    bars, features = _ca_cd_bars_and_features(0.20)

    events = detect_events(bars, features, SESSION_SET, ProbeParams())
    ca = _find(events, "C-A")
    assert len(ca) == 1
    assert ca[0].trading_day == date(2024, 1, 16)
    assert ca[0].direction == "none"
    assert ca[0].detail["pre_london_range_pct"] == pytest.approx(0.20)
    assert _find(events, "C-D") == []


def test_context_a_not_detected_when_range_pct_above_threshold():
    bars, features = _ca_cd_bars_and_features(0.50)

    events = detect_events(bars, features, SESSION_SET, ProbeParams())
    assert _find(events, "C-A") == []
    assert _find(events, "C-D") == []


# C-D ------------------------------------------------------------------


def test_context_d_detected_when_range_pct_high():
    bars, features = _ca_cd_bars_and_features(0.80)

    events = detect_events(bars, features, SESSION_SET, ProbeParams())
    cd = _find(events, "C-D")
    assert len(cd) == 1
    assert cd[0].direction == "none"
    assert _find(events, "C-A") == []


def test_context_a_and_d_null_when_no_bars_in_window():
    ts = _ns(2024, 1, 16, 20, 0)
    bars = _bars_frame([_bar_row(ts, 1.1000)])
    features = _features_frame([ts], range_pct=[None], pre_high=[None], pre_low=[None], atr=[None])

    events = detect_events(bars, features, SESSION_SET, ProbeParams())
    assert events == ()


# C-B --------------------------------------------------------------------


def _london_open_bars_and_features(mids: list[float], *, high: float, low: float, atr: float, start_minute: int = 480):
    """`mids` placed one per minute starting at 08:00 UTC (minute 480 of
    the day), i.e. inside london_open_window (08:00-11:00 London == UTC
    in January). Prepends one anchor bar at 07:00 -- inside pre_london's
    own window [00:00,08:00) -- carrying the same `high`/`low`/`atr`
    values: the D-083 anchored-extraction fix requires a real bar inside
    a session's own window for that day to be ELIGIBLE at all (matching
    pre_london_range_pct_compute's identical rule), distinct from which
    bar the VALUE is read from (the first bar at-or-after close, which
    for `high`/`low` here is this same anchor bar, constant across the
    whole day anyway). Without it, day_pre_high/day_pre_low would be NaN
    and every C-B/C-C event below would silently and wrongly not fire."""
    day_start = _ns(2024, 1, 16, 0, 0)
    anchor_ts = Nanos(day_start + 420 * NS_PER_MINUTE)  # 07:00 UTC
    event_ts_list = [Nanos(day_start + (start_minute + i) * NS_PER_MINUTE) for i in range(len(mids))]
    ts_list = [anchor_ts] + event_ts_list
    bars = _bars_frame([_bar_row(anchor_ts, mids[0])] + [_bar_row(t, m) for t, m in zip(event_ts_list, mids)])
    features = _features_frame(
        ts_list,
        range_pct=[None] * len(ts_list),
        pre_high=[high] * len(ts_list),
        pre_low=[low] * len(ts_list),
        atr=[atr] * len(ts_list),
    )
    return bars, features


def test_context_b_long_detected_on_break_above_high():
    high, low, atr = 1.1010, 1.0990, 0.0010
    # threshold = high + 0.10*atr = 1.1011; bar 2 breaks it.
    mids = [1.1000, 1.1005, 1.1030, 1.1005]
    bars, features = _london_open_bars_and_features(mids, high=high, low=low, atr=atr)

    events = detect_events(bars, features, SESSION_SET, ProbeParams())
    cb_long = _find(events, "C-B", "long")
    assert len(cb_long) == 1
    assert cb_long[0].detail["mid"] == pytest.approx(1.1030)
    assert _find(events, "C-B", "short") == []


def test_context_b_short_detected_on_break_below_low():
    high, low, atr = 1.1010, 1.0990, 0.0010
    # threshold = low - 0.10*atr = 1.0989; bar 2 breaks it.
    mids = [1.1000, 1.0995, 1.0970, 1.0995]
    bars, features = _london_open_bars_and_features(mids, high=high, low=low, atr=atr)

    events = detect_events(bars, features, SESSION_SET, ProbeParams())
    cb_short = _find(events, "C-B", "short")
    assert len(cb_short) == 1
    assert cb_short[0].detail["mid"] == pytest.approx(1.0970)
    assert _find(events, "C-B", "long") == []


def test_context_b_not_detected_within_buffer():
    high, low, atr = 1.1010, 1.0990, 0.0010
    # Never exceeds high + 0.10*atr = 1.1011.
    mids = [1.1000, 1.1005, 1.1008, 1.1005]
    bars, features = _london_open_bars_and_features(mids, high=high, low=low, atr=atr)

    events = detect_events(bars, features, SESSION_SET, ProbeParams())
    assert _find(events, "C-B") == []


def test_context_b_dedup_first_occurrence_only():
    """Many qualifying instants in one (day, C-B, direction) -- only the
    FIRST counts."""
    high, low, atr = 1.1010, 1.0990, 0.0010
    mids = [1.1030, 1.1035, 1.1040, 1.1032, 1.1038]  # all break the threshold
    bars, features = _london_open_bars_and_features(mids, high=high, low=low, atr=atr)

    events = detect_events(bars, features, SESSION_SET, ProbeParams())
    cb_long = _find(events, "C-B", "long")
    assert len(cb_long) == 1
    assert cb_long[0].detail["mid"] == pytest.approx(1.1030)  # the FIRST breaking bar


def test_context_b_day_with_both_directions_yields_two_events():
    high, low, atr = 1.1010, 1.0990, 0.0010
    mids = [1.1030, 1.1000, 1.0970, 1.1000]  # long break, then short break
    bars, features = _london_open_bars_and_features(mids, high=high, low=low, atr=atr)

    events = detect_events(bars, features, SESSION_SET, ProbeParams())
    assert len(_find(events, "C-B", "long")) == 1
    assert len(_find(events, "C-B", "short")) == 1


# C-C ----------------------------------------------------------------------


def test_context_c_detected_after_break_and_reentry():
    high, low, atr = 1.1010, 1.0990, 0.0010
    # bar1: break long (mid=1.1030 > 1.1010+0.1*0.001=1.1011)
    # bars2-3: still outside
    # bar4: re-entry (mid <= high - 0.05*atr = 1.1010-0.00005=1.10095)
    mids = [1.1030, 1.1025, 1.1020, 1.1005]
    bars, features = _london_open_bars_and_features(mids, high=high, low=low, atr=atr)

    events = detect_events(bars, features, SESSION_SET, ProbeParams())
    cc_long = _find(events, "C-C", "long")
    assert len(cc_long) == 1
    cb_long = _find(events, "C-B", "long")
    assert cc_long[0].ts_utc_ns == cb_long[0].ts_utc_ns  # timestamped at the C-B trigger


def test_context_c_not_detected_when_no_reentry():
    high, low, atr = 1.1010, 1.0990, 0.0010
    mids = [1.1030, 1.1032, 1.1035, 1.1040]  # never comes back
    bars, features = _london_open_bars_and_features(mids, high=high, low=low, atr=atr)

    events = detect_events(bars, features, SESSION_SET, ProbeParams())
    assert _find(events, "C-B", "long") != []
    assert _find(events, "C-C", "long") == []


def test_context_c_reentry_window_is_present_bars_not_calendar():
    """A break followed by a re-entry more than reentry_window_bars
    PRESENT bars later must not count -- construct exactly
    reentry_window_bars+1 bars between break and re-entry."""
    high, low, atr = 1.1010, 1.0990, 0.0010
    params = ProbeParams(reentry_window_bars=3)
    # break at bar0; bars 1-3 stay away; bar4 (the 4th bar after break) re-enters -- outside the 3-bar window.
    mids = [1.1030] + [1.1030] * 3 + [1.1005]
    bars, features = _london_open_bars_and_features(mids, high=high, low=low, atr=atr)

    events = detect_events(bars, features, SESSION_SET, params)
    assert _find(events, "C-C", "long") == []


# Deduplication (C-A/C-D are inherently single-evaluation; explicit dedup
# case for completeness) --------------------------------------------------


def test_dedup_context_a_single_event_even_with_multiple_bars_after_close():
    day_start = _ns(2024, 1, 16, 0, 0)
    anchor_ts = Nanos(day_start + 420 * NS_PER_MINUTE)  # inside pre_london's own window -- establishes eligibility
    after_close_ts = [Nanos(day_start + m * NS_PER_MINUTE) for m in (490, 500, 510)]
    ts_list = [anchor_ts] + after_close_ts
    bars = _bars_frame([_bar_row(t, 1.1000) for t in ts_list])
    features = _features_frame(
        ts_list, range_pct=[None, 0.20, 0.20, 0.20], pre_high=[None] * 4, pre_low=[None] * 4, atr=[None] * 4
    )

    events = detect_events(bars, features, SESSION_SET, ProbeParams())
    assert len(_find(events, "C-A")) == 1


# Errors / eligibility -------------------------------------------------


def test_detect_events_requires_feature_columns():
    ts = _ns(2024, 1, 16, 20, 0)
    bars = _bars_frame([_bar_row(ts, 1.1000)])
    bad_features = pl.DataFrame({"ts_utc_ns": [ts]}, schema={"ts_utc_ns": pl.Int64})
    with pytest.raises(ValueError):
        detect_events(bars, bad_features, SESSION_SET, ProbeParams())


def test_context_eligible_days():
    # Day 1 gets two bars: one inside its own pre_london window
    # [00:00,08:00) UTC (establishes eligibility under the D-083
    # anchored-extraction fix) and one strictly after its own close
    # (the value is read from the first bar AT OR AFTER close, a
    # different requirement from eligibility -- a single bar can never
    # satisfy both). Day 2 gets one bar inside its own window, with
    # null feature values -- it has real window presence but no
    # observable close-time value, so it stays correctly ineligible.
    day1_anchor = _ns(2024, 1, 16, 7, 0)
    day1_value = _ns(2024, 1, 16, 8, 10)
    day2_anchor = _ns(2024, 1, 17, 7, 0)

    bars = _bars_frame(
        [_bar_row(day1_anchor, 1.1000), _bar_row(day1_value, 1.1000), _bar_row(day2_anchor, 1.1000)]
    )
    features = _features_frame(
        [day1_anchor, day1_value, day2_anchor],
        range_pct=[None, 0.20, None],
        pre_high=[None, 1.1010, None],
        pre_low=[None, 1.0990, None],
        atr=[0.001, 0.001, None],
    )

    eligible = context_eligible_days(bars, features, SESSION_SET)
    assert date(2024, 1, 16) in eligible
    assert date(2024, 1, 17) not in eligible


# D-083 -- decisive proof of the anchored-extraction fix --------------
#
# _day_value's pre-fix implementation is reproduced here verbatim, purely
# to prove the fix is decisive the same way D-073's own decisive test was
# proven (features/library.py, D-073's writeup): reinstate the OLD logic
# in isolation and confirm it produces the exact predicted wrong number,
# rather than merely asserting the NEW logic looks right. Never called by
# any production code path.


def _old_day_value(features: pl.DataFrame, day_idx, n_days: int, column: str):
    import numpy as np

    tmp = pl.DataFrame({"_day_idx": day_idx, "_v": features[column]})
    agg = tmp.group_by("_day_idx", maintain_order=True).agg(
        pl.col("_v").drop_nulls().first().alias("_value")
    )
    out = np.full(n_days, np.nan, dtype=np.float64)
    if agg.height:
        out[agg["_day_idx"].to_numpy()] = agg["_value"].to_numpy()
    return out


def test_day_value_decisive_proof_old_logic_returns_yesterdays_value():
    # pre_london closes at exactly 08:00 UTC on both 2024-01-16 and
    # 2024-01-17 (verified directly: SESSION_SET.window('pre_london', d)
    # .end_ns for each date, winter, no DST offset). Four bars: one
    # inside day 1's own window (establishes day 1's eligibility under
    # the D-083 fix's rule; its own value is irrelevant, deliberately
    # None), one after day 1's own close (day 1's own true value, H1),
    # one inside day 2's own window, before day 2's own close (D-073's
    # "most recently completed instance" carry-forward: still H1, not
    # null), and one after day 2's own close (day 2's own true value,
    # H2, deliberately different from H1 so a day-shift bug is
    # impossible to miss).
    import numpy as np

    from jarvis.features.base import session_window_bounds, trading_day_boundaries

    day1_anchor = _ns(2024, 1, 16, 7, 0)  # inside day 1's own window
    day1_close = _ns(2024, 1, 16, 8, 10)  # after day 1's own close
    day2_early = _ns(2024, 1, 17, 1, 40)  # inside day 2's own window, well before its own close
    day2_late = _ns(2024, 1, 17, 8, 10)  # after day 2's own close

    ts_list = [day1_anchor, day1_close, day2_early, day2_late]
    bars = _bars_frame([_bar_row(t, 1.1000) for t in ts_list])
    H1, H2 = 1.1010, 1.2020
    features = _features_frame(
        ts_list,
        range_pct=[0.5, 0.5, 0.5, 0.5],
        pre_high=[None, H1, H1, H2],  # D-073 semantic: day 2's early bar carries H1 forward, not null
        pre_low=[1.0, 1.0, 1.0, 1.0],
        atr=[0.001, 0.001, 0.001, 0.001],
    )

    days, day_idx = trading_day_boundaries(bars)
    assert len(days) == 2
    n_days = len(days)

    # Prove the bug is real and predictable: the OLD logic, reinstated in
    # isolation, returns day 1's value for day 2 -- exactly the "first
    # non-null bar of the day" failure mode D-073's own docstring names.
    old_result = _old_day_value(features, day_idx, n_days, "pre_london_high")
    assert old_result[1] == pytest.approx(H1), (
        f"decisive-test sanity check failed: expected the OLD logic to "
        f"return day 1's carried-forward value ({H1}) for day 2 -- got "
        f"{old_result[1]}. If this assertion fails, the fixture itself "
        f"is not exercising the bug and the test below proves nothing."
    )

    # Prove the fix: the CURRENT (post-D-083) _day_value, anchored on
    # each day's own independently-computed window-close instant, returns
    # day 2's own value for day 2, and day 1's value for day 1 either way.
    from jarvis.probe.contexts import _day_value

    ts = bars["ts_utc_ns"].to_numpy()
    starts, ends = session_window_bounds(SESSION_SET, "pre_london", days)
    new_result = _day_value(ts, day_idx, n_days, starts, ends, features, "pre_london_high")
    assert new_result[0] == pytest.approx(H1)
    assert new_result[1] == pytest.approx(H2), (
        f"D-083 fix did not correct the day-shift bug: expected day 2's "
        f"own value ({H2}), got {new_result[1]}"
    )
