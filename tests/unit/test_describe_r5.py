from datetime import datetime, timezone

import numpy as np
import polars as pl
import pytest

from jarvis.core.types import Nanos
from jarvis.describe.r5 import compute_r5


def _synthetic_bars(n: int, *, start_utc: datetime, seed: int = 0) -> pl.DataFrame:
    """A BAR_SCHEMA-conformant frame of `n` consecutive 1-minute bars
    starting at `start_utc`, with a small, plausible bid/ask random walk
    -- shaped like a real resampled month, not a degenerate edge case."""
    rng = np.random.default_rng(seed)
    start_ns = int(start_utc.timestamp()) * 1_000_000_000
    ts = start_ns + np.arange(n) * 60_000_000_000
    bid_c = 1.3 + np.cumsum(rng.normal(0, 0.0001, n))
    ask_c = bid_c + rng.uniform(0.00005, 0.0003, n)
    spread_twa = ask_c - bid_c
    return pl.DataFrame(
        {
            "ts_utc_ns": ts,
            "bid_o": bid_c,
            "bid_h": bid_c,
            "bid_l": bid_c,
            "bid_c": bid_c,
            "ask_o": ask_c,
            "ask_h": ask_c,
            "ask_l": ask_c,
            "ask_c": ask_c,
            "tick_count": np.ones(n, dtype=np.int32),
            "first_tick_ns": ts,
            "last_tick_ns": ts,
            "spread_open": spread_twa,
            "spread_max": spread_twa,
            "spread_twa": spread_twa,
            "prev_gap_ns": np.zeros(n, dtype=np.int64),
        }
    )


def test_empty_bars_returns_empty_result():
    empty = pl.DataFrame(schema={c: pl.Float64 for c in ["ts_utc_ns", "bid_c", "ask_c", "spread_twa"]})
    result = compute_r5(empty, start_ns=Nanos(0), end_ns=Nanos(1))
    assert result.bars_examined == 0
    assert result.by_hour_of_week == ()
    assert result.by_year == ()


def test_bars_examined_matches_input_height():
    bars = _synthetic_bars(1000, start_utc=datetime(2010, 1, 4, tzinfo=timezone.utc))
    result = compute_r5(bars, start_ns=Nanos(0), end_ns=Nanos(1))
    assert result.bars_examined == 1000


def test_weekday_is_iso_monday_1_sunday_7():
    # 2010-01-04 is a real, confirmed Monday.
    bars = _synthetic_bars(60 * 24 * 8, start_utc=datetime(2010, 1, 4, tzinfo=timezone.utc))
    result = compute_r5(bars, start_ns=Nanos(0), end_ns=Nanos(1))
    weekdays = {row.weekday for row in result.by_hour_of_week}
    assert weekdays.issubset({1, 2, 3, 4, 5, 6, 7})
    assert 1 in weekdays  # Monday present


def test_hour_of_week_bucket_n_matches_manual_count():
    bars = _synthetic_bars(60 * 24 * 14, start_utc=datetime(2010, 1, 4, tzinfo=timezone.utc))
    result = compute_r5(bars, start_ns=Nanos(0), end_ns=Nanos(1))
    total_n = sum(row.n for row in result.by_hour_of_week)
    assert total_n == bars.height


def test_range_is_null_before_60_present_bars_accumulate():
    # The very first bar of the series cannot have a full trailing
    # 60-bar range yet -- confirms the rolling window requires a FULL
    # window (min_samples=60), not a partial-window estimate (Part 2
    # §F.1's "insufficient history yields null, never a partial-window
    # estimate" principle, applied here even though R5 is not a
    # registered feature).
    bars = _synthetic_bars(120, start_utc=datetime(2010, 1, 4, tzinfo=timezone.utc))
    result = compute_r5(bars, start_ns=Nanos(0), end_ns=Nanos(1))
    first_hour_row = next(r for r in result.by_hour_of_week if r.weekday == 1 and r.hour == 0)
    # n=60 bars fall in the very first Monday 00:00 bucket (00:00-00:59).
    # Only the 60th bar (index 59) has a full 60-bar trailing window; the
    # other 59 don't, so at most 1 of this bucket's 60 bars can have a
    # non-null range -- below the n<10 bootstrap floor, so the bucket's
    # range CI must be None even though its spread sample (n=60) is not.
    assert first_hour_row.n == 60
    assert first_hour_row.range_60m_ci is None


def test_bucket_below_min_n_has_no_bootstrap_ci():
    # A single bar's worth of data in one bucket -- far below the n<10
    # suppression floor.
    bars = _synthetic_bars(5, start_utc=datetime(2010, 1, 4, tzinfo=timezone.utc))
    result = compute_r5(bars, start_ns=Nanos(0), end_ns=Nanos(1))
    row = result.by_hour_of_week[0]
    assert row.n == 5
    assert row.spread_ci is None
    assert row.spread_to_range_ratio is None


def test_spread_to_range_ratio_is_spread_over_range():
    bars = _synthetic_bars(60 * 24 * 20, start_utc=datetime(2010, 1, 4, tzinfo=timezone.utc))
    result = compute_r5(bars, start_ns=Nanos(0), end_ns=Nanos(1))
    rows_with_ratio = [r for r in result.by_hour_of_week if r.spread_to_range_ratio is not None]
    assert rows_with_ratio, "expected at least one bucket with enough data for both CIs"
    for row in rows_with_ratio:
        expected = row.spread_ci.point_estimate / row.range_60m_ci.point_estimate
        assert row.spread_to_range_ratio == pytest.approx(expected)


def test_by_year_groups_use_utc_calendar_year():
    # Spans the 2010/2011 UTC year boundary.
    bars = _synthetic_bars(60 * 24 * 10, start_utc=datetime(2010, 12, 28, tzinfo=timezone.utc))
    result = compute_r5(bars, start_ns=Nanos(0), end_ns=Nanos(1))
    years = {row.year for row in result.by_year}
    assert years == {2010, 2011}


def test_rows_with_null_spread_twa_are_excluded():
    bars = _synthetic_bars(100, start_utc=datetime(2010, 1, 4, tzinfo=timezone.utc))
    bars = bars.with_columns(
        pl.when(pl.arange(0, pl.len()) < 10).then(None).otherwise(pl.col("spread_twa")).alias("spread_twa")
    )
    result = compute_r5(bars, start_ns=Nanos(0), end_ns=Nanos(1))
    total_n = sum(row.n for row in result.by_hour_of_week)
    assert total_n == 90


def test_deterministic_across_repeated_calls():
    bars = _synthetic_bars(60 * 24 * 5, start_utc=datetime(2010, 1, 4, tzinfo=timezone.utc))
    a = compute_r5(bars, start_ns=Nanos(0), end_ns=Nanos(1))
    b = compute_r5(bars, start_ns=Nanos(0), end_ns=Nanos(1))
    assert a.by_hour_of_week == b.by_hour_of_week
    assert a.by_year == b.by_year
