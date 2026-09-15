from datetime import datetime, timezone

import numpy as np
import polars as pl
import pytest

from jarvis.core.types import Nanos
from jarvis.describe.r2 import N_BUCKETS, _bootstrap_or_none, _bucket_quintiles, _bucket_stats, compute_r2
from jarvis.sessions import load_session_set

SESSION_SET = load_session_set("fx_core", 1)
NS_PER_MINUTE = 60_000_000_000


def _weekday_bars(n_weekdays: int, *, start: datetime, seed: int = 0) -> pl.DataFrame:
    """Same fixture shape as `test_describe_r1.py`'s own helper -- real
    weekday-shaped bars (Sat/Sun skipped), which R2 needs for the same
    reason R1 did (real session windows, real trading-day grouping)."""
    rng = np.random.default_rng(seed)
    rows = []
    day = start
    price = 1.3000
    added = 0
    while added < n_weekdays:
        if day.weekday() < 5:
            for m in range(24 * 60):
                ts = Nanos(int(day.timestamp()) * 1_000_000_000 + m * NS_PER_MINUTE)
                price += float(rng.normal(0, 0.00003))
                rows.append(
                    {
                        "ts_utc_ns": ts,
                        "bid_o": price,
                        "bid_h": price + 0.0002,
                        "bid_l": price - 0.0002,
                        "bid_c": price,
                        "ask_o": price + 0.0002,
                        "ask_h": price + 0.0004,
                        "ask_l": price,
                        "ask_c": price + 0.0002,
                        "tick_count": 1,
                        "first_tick_ns": ts,
                        "last_tick_ns": ts,
                        "spread_open": 0.0002,
                        "spread_max": 0.0002,
                        "spread_twa": 0.0002,
                        "prev_gap_ns": None,
                    }
                )
            added += 1
        day = datetime.fromtimestamp(day.timestamp() + 86400, tz=timezone.utc)
    return pl.DataFrame(rows)


# ---------------------------------------------------------------------------
# _bucket_quintiles: pure function, hand-verified against real behavior
# (see WP-021's own check-in) before these assertions were written.
# ---------------------------------------------------------------------------


def test_bucket_quintiles_empty_input():
    assert _bucket_quintiles(np.array([])).tolist() == []


def test_bucket_quintiles_no_ties_splits_evenly():
    vals = np.arange(20, dtype=float)
    bucket_ids = _bucket_quintiles(vals)
    for b in range(N_BUCKETS):
        assert (bucket_ids == b).sum() == 4


def test_bucket_quintiles_never_splits_a_tied_group():
    # 8 zeros (more than one bucket's worth of 4) followed by 12 unique
    # ascending values -- bucket 0's target of 4 is exceeded by the tied
    # group itself, so all 8 zeros stay together in bucket 0.
    vals = np.array([0] * 8 + list(range(1, 13)), dtype=float)
    bucket_ids = _bucket_quintiles(vals)
    zero_buckets = set(bucket_ids[vals == 0].tolist())
    assert zero_buckets == {0}, "a single tied value must never appear in more than one bucket"


def test_bucket_quintiles_is_monotonic_in_sorted_order():
    # Ascending input value must never map to a LOWER bucket id than an
    # earlier (smaller-or-equal) value -- bucket assignment must respect
    # the underlying order, not just group sizes.
    rng = np.random.default_rng(42)
    vals = rng.choice(np.arange(15, dtype=float), size=200, replace=True)
    bucket_ids = _bucket_quintiles(vals)
    order = np.argsort(vals, kind="stable")
    sorted_buckets = bucket_ids[order]
    assert np.all(np.diff(sorted_buckets) >= 0)


def test_bucket_quintiles_large_tied_group_can_starve_trailing_buckets():
    # A tied group so large it exceeds several buckets' worth of target
    # share leaves later buckets genuinely empty (n=0) -- this is the
    # real, demonstrated behavior (WP-021's check-in), not a hypothetical:
    # accepting the imbalance rather than forcing an artificial split is
    # the explicitly agreed tradeoff, and it must be visible, not patched
    # over silently here.
    vals = np.array(list(range(1, 11)) + [11] * 15, dtype=float)
    bucket_ids = _bucket_quintiles(vals)
    counts = [int((bucket_ids == b).sum()) for b in range(N_BUCKETS)]
    assert sum(counts) == vals.size
    assert counts[-1] == 0 or counts[-2] == 0  # at least one trailing bucket starved


def test_bucket_quintiles_every_input_assigned_exactly_once():
    rng = np.random.default_rng(7)
    vals = rng.integers(0, 61, size=137).astype(float)  # mimics the real 61-valued domain
    bucket_ids = _bucket_quintiles(vals)
    assert bucket_ids.shape == vals.shape
    assert set(bucket_ids.tolist()).issubset(set(range(N_BUCKETS)))


# ---------------------------------------------------------------------------
# _bootstrap_or_none: threshold behavior, direct and deterministic.
# ---------------------------------------------------------------------------


def test_bootstrap_or_none_below_threshold_is_none():
    assert _bootstrap_or_none(np.array([1.0] * 9), seed=1) is None


def test_bootstrap_or_none_at_threshold_returns_ci():
    result = _bootstrap_or_none(np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]), seed=1)
    assert result is not None
    assert result.n == 10


# ---------------------------------------------------------------------------
# compute_r2: structural properties over a real fixture.
# ---------------------------------------------------------------------------


def test_empty_bars_returns_empty_result():
    empty = pl.DataFrame(
        schema={
            "ts_utc_ns": pl.Int64, "bid_c": pl.Float64, "ask_c": pl.Float64,
            "bid_h": pl.Float64, "bid_l": pl.Float64, "ask_h": pl.Float64, "ask_l": pl.Float64,
            "bid_o": pl.Float64, "ask_o": pl.Float64, "tick_count": pl.Int32,
            "first_tick_ns": pl.Int64, "last_tick_ns": pl.Int64, "spread_open": pl.Float64,
            "spread_max": pl.Float64, "spread_twa": pl.Float64, "prev_gap_ns": pl.Int64,
        }
    )
    result = compute_r2(empty, SESSION_SET, start_ns=Nanos(0), end_ns=Nanos(1))
    assert result.bars_examined == 0
    assert result.pooled == ()
    assert result.by_year == ()


def test_below_warmup_has_no_eligible_days():
    # pre_london_range_pct(60) needs 60 prior eligible days -- far fewer
    # than that leaves no day with a non-null pct value at all.
    bars = _weekday_bars(10, start=datetime(2010, 1, 4, tzinfo=timezone.utc))
    result = compute_r2(bars, SESSION_SET, start_ns=Nanos(0), end_ns=Nanos(1))
    assert result.pooled == ()
    assert result.by_year == ()


def test_bars_examined_matches_input_height():
    bars = _weekday_bars(70, start=datetime(2010, 1, 4, tzinfo=timezone.utc))
    result = compute_r2(bars, SESSION_SET, start_ns=Nanos(0), end_ns=Nanos(1))
    assert result.bars_examined == bars.height


def test_deterministic_across_repeated_calls():
    bars = _weekday_bars(70, start=datetime(2010, 1, 4, tzinfo=timezone.utc))
    a = compute_r2(bars, SESSION_SET, start_ns=Nanos(0), end_ns=Nanos(1))
    b = compute_r2(bars, SESSION_SET, start_ns=Nanos(0), end_ns=Nanos(1))
    assert a.pooled == b.pooled
    assert a.by_year == b.by_year


def test_pooled_buckets_partition_all_eligible_days():
    bars = _weekday_bars(90, start=datetime(2010, 1, 4, tzinfo=timezone.utc))
    result = compute_r2(bars, SESSION_SET, start_ns=Nanos(0), end_ns=Nanos(1))
    assert len(result.pooled) == N_BUCKETS
    total_pooled_n = sum(b.n for b in result.pooled)
    assert total_pooled_n > 0

    # by_year must partition the SAME eligible days as pooled (same bucket
    # boundaries reused, just filtered by year -- WP-021's confirmed,
    # pooled-once bucketing) -- so the per-year totals must sum to the
    # pooled total exactly.
    total_year_n = sum(bs.n for yb in result.by_year for bs in yb.buckets)
    assert total_year_n == total_pooled_n


def test_pooled_bucket_pct_ranges_are_non_decreasing_in_bucket_order():
    # End-to-end confirmation that ascending pct order -> non-decreasing
    # bucket id actually holds through the full compute_r2 pipeline, not
    # just the isolated _bucket_quintiles unit tests above.
    bars = _weekday_bars(90, start=datetime(2010, 1, 4, tzinfo=timezone.utc))
    result = compute_r2(bars, SESSION_SET, start_ns=Nanos(0), end_ns=Nanos(1))
    populated = [b for b in result.pooled if b.n > 0]
    for a, b in zip(populated, populated[1:]):
        assert a.pct_max <= b.pct_min


def test_bucket_stats_has_iqr_and_pct_range_when_populated():
    bars = _weekday_bars(90, start=datetime(2010, 1, 4, tzinfo=timezone.utc))
    result = compute_r2(bars, SESSION_SET, start_ns=Nanos(0), end_ns=Nanos(1))
    for bs in result.pooled:
        if bs.n == 0:
            assert bs.pct_min is None and bs.pct_max is None
            assert bs.q1_ratio is None and bs.q3_ratio is None
        else:
            assert bs.pct_min is not None and bs.pct_max is not None
            assert bs.pct_min <= bs.pct_max
            assert bs.q1_ratio is not None and bs.q3_ratio is not None
            assert bs.q1_ratio <= bs.q3_ratio


def _flat_bid_fluctuating_ask_bars(n_weekdays: int, *, start: datetime, fluctuate_days: set) -> pl.DataFrame:
    """Same construction as `test_describe_r1.py`'s own helper: bid is
    bit-identical on every bar in the whole fixture (true_range stays
    exactly 0, so atr_bars(1440) is exactly 0.0 from its warmup bar
    onward), while ask alternates on every calendar day in
    `fluctuate_days` (0-indexed weekday counters), so that whichever real
    trading day (17:00 America/New_York boundaries, offset from
    UTC-midnight calendar days) ends up covering london's own window, its
    range is genuinely nonzero despite atr_bars being exactly 0.0 there --
    the day_range>0, day_atr==0.0 exactly scenario the isinf fix protects
    against, constructed directly rather than hoped for. A multi-day span
    (not a single day) is used deliberately so the result does not depend
    on precisely knowing the trading-day/calendar-day offset."""
    rows = []
    day = start
    added = 0
    day_num = 0
    while added < n_weekdays:
        if day.weekday() < 5:
            for m in range(24 * 60):
                ts = Nanos(int(day.timestamp()) * 1_000_000_000 + m * NS_PER_MINUTE)
                bid = 1.3000
                ask = (1.3002 if m % 2 == 0 else 1.3010) if day_num in fluctuate_days else 1.3002
                rows.append(
                    {
                        "ts_utc_ns": ts, "bid_o": bid, "bid_h": bid, "bid_l": bid, "bid_c": bid,
                        "ask_o": ask, "ask_h": ask, "ask_l": ask, "ask_c": ask,
                        "tick_count": 1, "first_tick_ns": ts, "last_tick_ns": ts,
                        "spread_open": ask - bid, "spread_max": ask - bid, "spread_twa": ask - bid,
                        "prev_gap_ns": None,
                    }
                )
            added += 1
            day_num += 1
        day = datetime.fromtimestamp(day.timestamp() + 86400, tz=timezone.utc)
    return pl.DataFrame(rows)


def test_exact_zero_atr_day_excluded_not_infinite():
    # The last 20 calendar weekdays (well past pre_london_range_pct's own
    # 60-day warmup) fluctuate. Verified directly (not assumed) that this
    # produces real trading days with london_range > 0 while atr_bars is
    # exactly 0.0 -- the calendar-weekday counter used to build this
    # fixture and the 17:00-America/New_York-anchored trading-day index
    # `compute_r2` actually uses are offset from each other, so a wide
    # fluctuation span is used rather than one hand-picked day index.
    bars = _flat_bid_fluctuating_ask_bars(
        75, start=datetime(2010, 1, 4, tzinfo=timezone.utc), fluctuate_days=set(range(55, 75))
    )
    result = compute_r2(bars, SESSION_SET, start_ns=Nanos(0), end_ns=Nanos(1))

    for bs in result.pooled:
        if bs.q1_ratio is not None:
            assert np.isfinite(bs.q1_ratio) and np.isfinite(bs.q3_ratio)
        if bs.median_ratio is not None:
            assert np.isfinite(bs.median_ratio.point_estimate)
            assert np.isfinite(bs.median_ratio.ci_low) and np.isfinite(bs.median_ratio.ci_high)
    for yb in result.by_year:
        for bs in yb.buckets:
            if bs.q1_ratio is not None:
                assert np.isfinite(bs.q1_ratio) and np.isfinite(bs.q3_ratio)


def test_bucket_stats_empty_arrays_returns_all_none():
    # _bucket_stats's own n==0 branch, tested directly. A real (if rare on
    # real data) possibility per the demonstrated starvation behaviour of
    # _bucket_quintiles above -- tested here at the level that actually
    # constructs a BucketStats, not just the bucket-id assignment.
    bs = _bucket_stats(4, np.array([]), np.array([]), seed=1)
    assert bs.bucket == 4
    assert bs.n == 0
    assert bs.pct_min is None and bs.pct_max is None
    assert bs.median_ratio is None
    assert bs.q1_ratio is None and bs.q3_ratio is None
