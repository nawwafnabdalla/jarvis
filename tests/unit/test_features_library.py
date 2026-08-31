import math
from datetime import date, datetime, timezone

import polars as pl
import pytest

from jarvis.core.errors import UserError
from jarvis.core.types import Nanos
from jarvis.features import REGISTRY, FeatureContext, FeatureDef, LookbackSpec, compute, register, resolve_order
from jarvis.features.library import atr_bars_compute, rv_60m_compute

# Plain-module import (not a test-to-test import) -- see _feature_fixtures's
# own docstring for why this must not be `from tests.unit._feature_fixtures`.
from _feature_fixtures import (
    GAP_TOLERANCE_NS,
    NS_PER_HOUR,
    NS_PER_MINUTE,
    SESSION_SET as _SESSION_SET,
    build_fixture_bars,
    frame as _frame,
    multi_day_pre_london as _multi_day_pre_london,
    ns as _ns,
    row as _row,
    weekdays_from as _weekdays_from,
)


# atr_bars --------------------------------------------------------------


def test_atr_hand_computed():
    """5-bar fixture, n=3. TR (bid series, first bar TR=H-L):
      TR0 = 10-8 = 2
      TR1 = max(11-9, |11-9|, |9-9|) = max(2,2,0) = 2
      TR2 = max(9-7, |9-10|, |7-10|) = max(2,1,3) = 3
      TR3 = max(10-8, |10-8|, |8-8|) = max(2,2,0) = 2
      TR4 = max(11-9, |11-9|, |9-9|) = max(2,2,0) = 2
    Seed (mean of TR0..TR2) = (2+2+3)/3 = 2.333... -> ATR[2].
    ATR[3] = (2*2.333... + 2)/3 = 2.222...
    ATR[4] = (2*2.222... + 2)/3 = 2.148...
    """
    bars = _frame(
        [
            _row(_ns(2024, 1, 9, 0, 0), bid_h=10, bid_l=8, bid_c=9),
            _row(_ns(2024, 1, 9, 0, 1), bid_h=11, bid_l=9, bid_c=10),
            _row(_ns(2024, 1, 9, 0, 2), bid_h=9, bid_l=7, bid_c=8),
            _row(_ns(2024, 1, 9, 0, 3), bid_h=10, bid_l=8, bid_c=9),
            _row(_ns(2024, 1, 9, 0, 4), bid_h=11, bid_l=9, bid_c=10),
        ]
    )
    ctx = FeatureContext(bars=bars, computed={}, session_set=_SESSION_SET, params={"n": 3})
    result = atr_bars_compute(ctx).to_list()

    assert result[0] is None
    assert result[1] is None
    assert result[2] == pytest.approx(7 / 3, abs=1e-9)
    assert result[3] == pytest.approx((2 * (7 / 3) + 2) / 3, abs=1e-9)
    assert result[4] == pytest.approx((2 * ((2 * (7 / 3) + 2) / 3) + 2) / 3, abs=1e-9)


def test_atr_counts_present_bars_not_clock_minutes():
    """Same 6 bars, same TR sequence, n=5: one fixture contiguous, one
    with a 3-hour gap inserted between bars 2 and 3. ATR must be
    identical -- it counts bar POSITIONS, never elapsed clock time."""
    specs = [
        (10, 8, 9),
        (11, 9, 10),
        (9, 7, 8),
        (10, 8, 9),
        (11, 9, 10),
        (12, 10, 11),
    ]

    contiguous = _frame(
        [_row(_ns(2024, 1, 9, 0, i), bid_h=h, bid_l=l, bid_c=c) for i, (h, l, c) in enumerate(specs)]
    )

    gapped_rows = []
    for i, (h, l, c) in enumerate(specs):
        ts = _ns(2024, 1, 9, 0, i) if i < 3 else Nanos(_ns(2024, 1, 9, 0, 3) + (i - 3) * NS_PER_MINUTE + 3 * NS_PER_HOUR)
        gapped_rows.append(_row(ts, bid_h=h, bid_l=l, bid_c=c))
    gapped = _frame(gapped_rows)

    ctx_a = FeatureContext(bars=contiguous, computed={}, session_set=_SESSION_SET, params={"n": 5})
    ctx_b = FeatureContext(bars=gapped, computed={}, session_set=_SESSION_SET, params={"n": 5})

    result_a = atr_bars_compute(ctx_a).to_list()
    result_b = atr_bars_compute(ctx_b).to_list()
    assert result_a == pytest.approx(result_b, nan_ok=True)


def test_atr_null_during_warmup():
    """With exactly n-1 bars, the output is entirely null (L-4's own
    definition); with exactly n bars, the last one is the first non-null
    value (see atr_bars_compute's docstring for why n-1, not n, bars are
    null -- the mathematically forced indexing)."""
    n = 5
    rows = [_row(_ns(2024, 1, 9, 0, i), bid_h=10 + i, bid_l=8 + i, bid_c=9 + i) for i in range(n - 1)]
    ctx = FeatureContext(bars=_frame(rows), computed={}, session_set=_SESSION_SET, params={"n": n})
    result = atr_bars_compute(ctx).to_list()
    assert all(v is None for v in result)

    rows_full = rows + [_row(_ns(2024, 1, 9, 0, n - 1), bid_h=10 + n, bid_l=8 + n, bid_c=9 + n)]
    ctx_full = FeatureContext(bars=_frame(rows_full), computed={}, session_set=_SESSION_SET, params={"n": n})
    result_full = atr_bars_compute(ctx_full).to_list()
    assert all(v is None for v in result_full[: n - 1])
    assert result_full[n - 1] is not None


# pre_london_high / pre_london_low / pre_london_range -----------------------


def _pre_london_frame(day: date, *, extra_after_close: bool = True) -> pl.DataFrame:
    """One trading day with bars every minute through the pre_london
    window (00:00-08:00 UTC in January) and a few bars after close."""
    day_start = Nanos(int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()) * 1_000_000_000)
    rows = []
    for m in range(8 * 60):
        ts = Nanos(day_start + m * NS_PER_MINUTE)
        price = 1.1000 + 0.0001 * math.sin(m / 17.0)
        rows.append(_row(ts, bid_h=price + 0.0003, bid_l=price - 0.0003, bid_c=price))
    if extra_after_close:
        for m in range(3):
            ts = Nanos(day_start + (8 * 60 + m) * NS_PER_MINUTE)
            rows.append(_row(ts, bid_h=1.1005, bid_l=1.0995, bid_c=1.1000))
    return _frame(rows)


def test_pre_london_null_before_window_close():
    bars = _pre_london_frame(date(2024, 1, 15))
    result = compute(["pre_london_high", "pre_london_low", "pre_london_range"], bars, _SESSION_SET)

    ts_0300 = _ns(2024, 1, 15, 3, 0)
    row = result.frame.filter(pl.col("ts_utc_ns") == ts_0300).row(0, named=True)
    assert row["pre_london_high"] is None
    assert row["pre_london_low"] is None
    assert row["pre_london_range"] is None


def test_pre_london_constant_after_window_close():
    bars = _pre_london_frame(date(2024, 1, 15))
    result = compute(["pre_london_high", "pre_london_low", "pre_london_range"], bars, _SESSION_SET)

    after = result.frame.filter(pl.col("ts_utc_ns") >= _ns(2024, 1, 15, 8, 0))
    assert after.height > 0
    assert after["pre_london_high"].n_unique() == 1
    assert after["pre_london_low"].n_unique() == 1
    assert after["pre_london_range"].n_unique() == 1
    assert after["pre_london_high"].null_count() == 0


def test_pre_london_null_when_window_has_no_bars():
    day = date(2024, 1, 15)
    day_start = _ns(2024, 1, 15, 0, 0)
    # Bars only from 09:00 onward -- nothing inside [00:00, 08:00).
    rows = []
    for m in range(60):
        ts = Nanos(day_start + (9 * 60 + m) * NS_PER_MINUTE)
        rows.append(_row(ts, bid_h=1.1005, bid_l=1.0995, bid_c=1.1000))
    bars = _frame(rows)

    result = compute(["pre_london_high", "pre_london_low", "pre_london_range"], bars, _SESSION_SET)
    assert result.frame["pre_london_high"].null_count() == result.frame.height
    assert result.frame["pre_london_low"].null_count() == result.frame.height
    assert result.frame["pre_london_range"].null_count() == result.frame.height


def test_pre_london_uses_mid_not_bid_or_ask():
    """Tick A has the highest bid but a low ask; tick B has a low bid but
    the highest ask. Neither the bid-only nor ask-only extreme equals the
    mid extreme -- only a genuine mid computation gives the values
    asserted here."""
    day_start = _ns(2024, 1, 15, 0, 0)
    rows = [
        _row(Nanos(day_start), bid_h=1.1050, bid_l=1.1050, bid_c=1.1050, ask_c=1.1052, ask_h=1.1052, ask_l=1.1052),
        _row(
            Nanos(day_start + NS_PER_MINUTE),
            bid_h=1.0950,
            bid_l=1.0950,
            bid_c=1.0950,
            ask_c=1.1150,
            ask_h=1.1150,
            ask_l=1.1150,
        ),
        # After window close, so the day's terminal value is observable.
        _row(Nanos(day_start + 8 * 60 * NS_PER_MINUTE), bid_h=1.1000, bid_l=1.1000, bid_c=1.1000, ask_c=1.1002),
    ]
    bars = _frame(rows)
    result = compute(["pre_london_high", "pre_london_low"], bars, _SESSION_SET)

    after = result.frame.filter(pl.col("ts_utc_ns") >= _ns(2024, 1, 15, 8, 0))
    # mid_a = (1.1050+1.1052)/2 = 1.1051; mid_b = (1.0950+1.1150)/2 = 1.1050
    assert after["pre_london_high"][0] == pytest.approx(1.1051, abs=1e-9)
    assert after["pre_london_low"][0] == pytest.approx(1.1050, abs=1e-9)
    # Neither equals the bid-only extremes (1.1050/1.0950) or ask-only (1.1150/1.1052).
    assert after["pre_london_high"][0] not in (pytest.approx(1.1050), pytest.approx(1.1150))
    assert after["pre_london_low"][0] not in (pytest.approx(1.0950), pytest.approx(1.1052))


# pre_london_range_pct -------------------------------------------------------




def test_range_pct_excludes_today():
    """Today's range is the maximum ever seen. pct == 1.0 is reachable
    ONLY if today is excluded from its own reference distribution -- if
    it were included, the best today could ever score is 60/61, never
    a clean 1.0."""
    days = _weekdays_from(date(2024, 1, 1), 61)
    day_ranges = [(d, 0.0010 + 0.00001 * i) for i, d in enumerate(days[:60])]
    day_ranges.append((days[60], 0.01))  # today: far larger than every prior day
    bars = _multi_day_pre_london(day_ranges)

    result = compute(["pre_london_range_pct"], bars, _SESSION_SET)
    today_start, today_end = _ns(days[60].year, days[60].month, days[60].day, 8, 0), None
    row = result.frame.filter(pl.col("ts_utc_ns") >= today_start).row(0, named=True)
    assert row["pre_london_range_pct"] == pytest.approx(1.0, abs=1e-9)


def test_range_pct_null_below_60_prior_days():
    days_59 = _weekdays_from(date(2024, 1, 1), 60)  # 59 priors + 1 today
    day_ranges_59 = [(d, 0.0010 + 0.00001 * i) for i, d in enumerate(days_59[:59])]
    day_ranges_59.append((days_59[59], 0.005))
    bars_59 = _multi_day_pre_london(day_ranges_59)
    result_59 = compute(["pre_london_range_pct"], bars_59, _SESSION_SET)
    today_59 = _ns(days_59[59].year, days_59[59].month, days_59[59].day, 8, 0)
    row_59 = result_59.frame.filter(pl.col("ts_utc_ns") >= today_59).row(0, named=True)
    assert row_59["pre_london_range_pct"] is None

    days_60 = _weekdays_from(date(2024, 1, 1), 61)  # 60 priors + 1 today
    day_ranges_60 = [(d, 0.0010 + 0.00001 * i) for i, d in enumerate(days_60[:60])]
    day_ranges_60.append((days_60[60], 0.005))
    bars_60 = _multi_day_pre_london(day_ranges_60)
    result_60 = compute(["pre_london_range_pct"], bars_60, _SESSION_SET)
    today_60 = _ns(days_60[60].year, days_60[60].month, days_60[60].day, 8, 0)
    row_60 = result_60.frame.filter(pl.col("ts_utc_ns") >= today_60).row(0, named=True)
    assert row_60["pre_london_range_pct"] is not None


def test_range_pct_tie_uses_strict_less_than():
    """10 prior days tie exactly with today's range, 50 are strictly
    below. pct must be 50/60, not 60/60 -- ties do not count as
    'exceeded'."""
    days = _weekdays_from(date(2024, 1, 1), 61)
    today_range = 0.0050
    day_ranges = [(d, 0.0010 + 0.00001 * i) for i, d in enumerate(days[:50])]  # all strictly below
    day_ranges += [(d, today_range) for d in days[50:60]]  # 10 exact ties
    day_ranges.append((days[60], today_range))  # today
    bars = _multi_day_pre_london(day_ranges)

    result = compute(["pre_london_range_pct"], bars, _SESSION_SET)
    today_ns = _ns(days[60].year, days[60].month, days[60].day, 8, 0)
    row = result.frame.filter(pl.col("ts_utc_ns") >= today_ns).row(0, named=True)
    assert row["pre_london_range_pct"] == pytest.approx(50 / 60, abs=1e-9)


# rv_60m ------------------------------------------------------------------


def test_rv_hand_computed():
    """4 bars, n=3. mid = [1.0, 1.0010, 1.0005, 1.0020].
    ret1 = ln(1.0010/1.0), ret2 = ln(1.0005/1.0010), ret3 = ln(1.0020/1.0005).
    First valid window is at bar index n=3: sqrt(ret1^2+ret2^2+ret3^2)."""
    mids = [1.0, 1.0010, 1.0005, 1.0020]
    rows = [
        _row(_ns(2024, 1, 9, 0, i), bid_h=m, bid_l=m, bid_c=m, ask_c=m)
        for i, m in enumerate(mids)
    ]
    bars = _frame(rows)
    ctx = FeatureContext(
        bars=bars, computed={}, session_set=_SESSION_SET, params={"n": 3, "gap_tolerance_ns": GAP_TOLERANCE_NS}
    )
    result = rv_60m_compute(ctx).to_list()

    ret1 = math.log(mids[1] / mids[0])
    ret2 = math.log(mids[2] / mids[1])
    ret3 = math.log(mids[3] / mids[2])
    expected = math.sqrt(ret1**2 + ret2**2 + ret3**2)

    assert result[0] is None
    assert result[1] is None
    assert result[2] is None
    assert result[3] == pytest.approx(expected, abs=1e-9)


def test_rv_nulls_across_gap():
    n = 3
    mids = [1.0, 1.0010, 1.0005, 1.0020]
    ts0 = _ns(2024, 1, 9, 0, 0)
    rows_normal = [
        _row(Nanos(ts0 + i * NS_PER_MINUTE), bid_h=m, bid_l=m, bid_c=m, ask_c=m, prev_gap_ns=(None if i == 0 else NS_PER_MINUTE))
        for i, m in enumerate(mids)
    ]
    ctx_normal = FeatureContext(
        bars=_frame(rows_normal), computed={}, session_set=_SESSION_SET,
        params={"n": n, "gap_tolerance_ns": GAP_TOLERANCE_NS},
    )
    assert rv_60m_compute(ctx_normal).to_list()[3] is not None

    big_gap = GAP_TOLERANCE_NS + NS_PER_MINUTE
    rows_gapped = [
        _row(Nanos(ts0), bid_h=mids[0], bid_l=mids[0], bid_c=mids[0], ask_c=mids[0], prev_gap_ns=None),
        _row(Nanos(ts0 + NS_PER_MINUTE), bid_h=mids[1], bid_l=mids[1], bid_c=mids[1], ask_c=mids[1], prev_gap_ns=big_gap),
        _row(Nanos(ts0 + 2 * NS_PER_MINUTE), bid_h=mids[2], bid_l=mids[2], bid_c=mids[2], ask_c=mids[2], prev_gap_ns=NS_PER_MINUTE),
        _row(Nanos(ts0 + 3 * NS_PER_MINUTE), bid_h=mids[3], bid_l=mids[3], bid_c=mids[3], ask_c=mids[3], prev_gap_ns=NS_PER_MINUTE),
    ]
    ctx_gapped = FeatureContext(
        bars=_frame(rows_gapped), computed={}, session_set=_SESSION_SET,
        params={"n": n, "gap_tolerance_ns": GAP_TOLERANCE_NS},
    )
    assert rv_60m_compute(ctx_gapped).to_list()[3] is None


# resolve_order / register ---------------------------------------------------


def _dummy_defn(name: str, requires: tuple[str, ...] = ()) -> FeatureDef:
    return FeatureDef(
        name=name,
        version=1,
        dtype=pl.Float64,
        lookback=LookbackSpec("bars", 1),
        gap_tolerance_ns=None,
        requires=requires,
        params={},
        leakage_class="causal",
        compute=lambda ctx: ctx.bars["ts_utc_ns"].cast(pl.Float64),
    )


def test_resolve_order_topological():
    names = ["_test_topo_c", "_test_topo_b", "_test_topo_a"]
    try:
        register(_dummy_defn("_test_topo_c"))
        register(_dummy_defn("_test_topo_b", requires=("_test_topo_c",)))
        register(_dummy_defn("_test_topo_a", requires=("_test_topo_b",)))

        order = resolve_order(["_test_topo_a"])
        assert order == ("_test_topo_c", "_test_topo_b", "_test_topo_a")
    finally:
        for name in names:
            REGISTRY.pop(name, None)


def test_resolve_order_detects_cycle():
    """A genuine cycle cannot be constructed through register() itself
    (it requires each dependency to already be registered, so A -> B ->
    A can never both be registered in either order). resolve_order must
    still defend against a cycle reaching REGISTRY some other way, so
    this test inserts one directly."""
    try:
        REGISTRY["_test_cycle_a"] = _dummy_defn("_test_cycle_a", requires=("_test_cycle_b",))
        REGISTRY["_test_cycle_b"] = _dummy_defn("_test_cycle_b", requires=("_test_cycle_a",))
        with pytest.raises(UserError):
            resolve_order(["_test_cycle_a"])
    finally:
        REGISTRY.pop("_test_cycle_a", None)
        REGISTRY.pop("_test_cycle_b", None)


def test_register_rejects_unknown_dependency():
    with pytest.raises(UserError):
        register(_dummy_defn("_test_unknown_dep", requires=("_does_not_exist",)))


# write_features merge semantics (D-045) -------------------------------------


def test_write_features_month_part_a_then_b_preserves_both(tmp_path):
    """Mirrors test_resampling_second_day_preserves_first
    (WP-005-CORRECTION): writing month-part-A then month-part-B must
    preserve both -- a sub-range write must never destroy the rest of an
    already-written month (D-045)."""
    from jarvis.features import features_path, write_features

    day1 = _ns(2024, 1, 15, 8, 0)
    day2 = _ns(2024, 1, 16, 8, 0)
    schema = {"ts_utc_ns": pl.Int64, "atr_bars": pl.Float64}
    frame_a = pl.DataFrame({"ts_utc_ns": [day1], "atr_bars": [1.0]}, schema=schema)
    frame_b = pl.DataFrame({"ts_utc_ns": [day2], "atr_bars": [2.0]}, schema=schema)

    write_features(tmp_path, "GBPUSD", 2024, 1, frame_a)
    write_features(tmp_path, "GBPUSD", 2024, 1, frame_b)

    result = pl.read_parquet(features_path(tmp_path, "GBPUSD", 2024, 1)).sort("ts_utc_ns")
    assert result.height == 2
    assert result["ts_utc_ns"].to_list() == [day1, day2]
    assert result["atr_bars"].to_list() == [1.0, 2.0]


def test_write_features_recompute_overwrites_same_bar(tmp_path):
    from jarvis.features import features_path, write_features

    day1 = _ns(2024, 1, 15, 8, 0)
    schema = {"ts_utc_ns": pl.Int64, "atr_bars": pl.Float64}

    write_features(tmp_path, "GBPUSD", 2024, 1, pl.DataFrame({"ts_utc_ns": [day1], "atr_bars": [1.0]}, schema=schema))
    write_features(tmp_path, "GBPUSD", 2024, 1, pl.DataFrame({"ts_utc_ns": [day1], "atr_bars": [9.0]}, schema=schema))

    result = pl.read_parquet(features_path(tmp_path, "GBPUSD", 2024, 1))
    assert result.height == 1
    assert result["atr_bars"].to_list() == [9.0]
