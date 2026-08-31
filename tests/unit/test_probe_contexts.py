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


def test_context_a_detected_when_range_pct_low():
    # 2024-01-16 is a Tuesday. A lone bar at 20:00 (well clear of both
    # pre_london and london_open_window) so only C-A/C-D are in play.
    ts = _ns(2024, 1, 16, 20, 0)
    bars = _bars_frame([_bar_row(ts, 1.1000)])
    features = _features_frame([ts], range_pct=[0.20], pre_high=[None], pre_low=[None], atr=[None])

    events = detect_events(bars, features, SESSION_SET, ProbeParams())
    ca = _find(events, "C-A")
    assert len(ca) == 1
    assert ca[0].trading_day == date(2024, 1, 16)
    assert ca[0].direction == "none"
    assert ca[0].detail["pre_london_range_pct"] == pytest.approx(0.20)
    assert _find(events, "C-D") == []


def test_context_a_not_detected_when_range_pct_above_threshold():
    ts = _ns(2024, 1, 16, 20, 0)
    bars = _bars_frame([_bar_row(ts, 1.1000)])
    features = _features_frame([ts], range_pct=[0.50], pre_high=[None], pre_low=[None], atr=[None])

    events = detect_events(bars, features, SESSION_SET, ProbeParams())
    assert _find(events, "C-A") == []
    assert _find(events, "C-D") == []


# C-D ------------------------------------------------------------------


def test_context_d_detected_when_range_pct_high():
    ts = _ns(2024, 1, 16, 20, 0)
    bars = _bars_frame([_bar_row(ts, 1.1000)])
    features = _features_frame([ts], range_pct=[0.80], pre_high=[None], pre_low=[None], atr=[None])

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
    in January)."""
    day_start = _ns(2024, 1, 16, 0, 0)
    ts_list = [Nanos(day_start + (start_minute + i) * NS_PER_MINUTE) for i in range(len(mids))]
    bars = _bars_frame([_bar_row(t, m) for t, m in zip(ts_list, mids)])
    features = _features_frame(
        ts_list,
        range_pct=[None] * len(mids),
        pre_high=[high] * len(mids),
        pre_low=[low] * len(mids),
        atr=[atr] * len(mids),
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
    ts_list = [Nanos(day_start + m * NS_PER_MINUTE) for m in (490, 500, 510)]
    bars = _bars_frame([_bar_row(t, 1.1000) for t in ts_list])
    features = _features_frame(
        ts_list, range_pct=[0.20, 0.20, 0.20], pre_high=[None] * 3, pre_low=[None] * 3, atr=[None] * 3
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
    day_start = _ns(2024, 1, 16, 0, 0)
    ts_eligible = Nanos(day_start + 490 * NS_PER_MINUTE)
    ts_ineligible = Nanos(_ns(2024, 1, 17, 0, 0) + 490 * NS_PER_MINUTE)

    bars = _bars_frame([_bar_row(ts_eligible, 1.1000), _bar_row(ts_ineligible, 1.1000)])
    features = _features_frame(
        [ts_eligible, ts_ineligible],
        range_pct=[0.20, None],
        pre_high=[1.1010, None],
        pre_low=[1.0990, None],
        atr=[0.001, None],
    )

    eligible = context_eligible_days(bars, features, SESSION_SET)
    assert date(2024, 1, 16) in eligible
    assert date(2024, 1, 17) not in eligible
