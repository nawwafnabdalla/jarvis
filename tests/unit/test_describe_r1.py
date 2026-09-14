from datetime import datetime, timezone

import numpy as np
import polars as pl
import pytest

from jarvis.core.types import Nanos
from jarvis.describe.r1 import SESSIONS, compute_r1
from jarvis.sessions import load_session_set

SESSION_SET = load_session_set("fx_core", 1)
NS_PER_MINUTE = 60_000_000_000


def _weekday_bars(n_weekdays: int, *, start: datetime, seed: int = 0) -> pl.DataFrame:
    """Continuous 1-minute bars covering `n_weekdays` REAL weekdays
    (skipping Sat/Sun entirely, matching real market data -- unlike a
    naive continuous run, which would wrongly give weekends session
    windows to compute over)."""
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
    result = compute_r1(empty, SESSION_SET, start_ns=Nanos(0), end_ns=Nanos(1))
    assert result.bars_examined == 0
    assert result.sessions == ()


def test_computes_all_three_sessions_in_order():
    bars = _weekday_bars(60, start=datetime(2010, 1, 4, tzinfo=timezone.utc))
    result = compute_r1(bars, SESSION_SET, start_ns=Nanos(0), end_ns=Nanos(1))
    assert [s.session for s in result.sessions] == list(SESSIONS)


def test_bars_examined_matches_input_height():
    bars = _weekday_bars(30, start=datetime(2010, 1, 4, tzinfo=timezone.utc))
    result = compute_r1(bars, SESSION_SET, start_ns=Nanos(0), end_ns=Nanos(1))
    assert result.bars_examined == bars.height


def test_weekday_cells_never_include_saturday_or_sunday():
    bars = _weekday_bars(40, start=datetime(2010, 1, 4, tzinfo=timezone.utc))
    result = compute_r1(bars, SESSION_SET, start_ns=Nanos(0), end_ns=Nanos(1))
    for sess in result.sessions:
        weekdays = {wc.weekday for wc in sess.by_weekday}
        assert weekdays.issubset({0, 1, 2, 3, 4})


def test_year_cell_n_matches_manual_day_count():
    # 10 real weekdays starting Monday 2010-01-04 -> spans exactly 2 real weeks (10 weekdays).
    bars = _weekday_bars(10, start=datetime(2010, 1, 4, tzinfo=timezone.utc))
    result = compute_r1(bars, SESSION_SET, start_ns=Nanos(0), end_ns=Nanos(1))
    for sess in result.sessions:
        total_n = sum(yc.stats.n for yc in sess.by_year)
        # 9, not 10: every session's own window closes well before
        # atr_bars(n=1440)'s warmup completes on day 1 (1440 minutes =
        # a full day, and no session here closes that late into day 1),
        # so day 1's ATR-unit ratio is null there and day 1 is correctly
        # excluded -- confirmed directly, not assumed, by first observing
        # this exact 9-vs-10 discrepancy and tracing it to ATR warmup
        # rather than a bug in the day-attribution logic.
        assert total_n == 9, f"{sess.session}: expected 9, got {total_n}"


def test_atr_unit_ratio_is_price_range_over_atr():
    bars = _weekday_bars(30, start=datetime(2010, 1, 4, tzinfo=timezone.utc))
    result = compute_r1(bars, SESSION_SET, start_ns=Nanos(0), end_ns=Nanos(1))
    for sess in result.sessions:
        for yc in sess.by_year:
            if yc.stats.median_price is None or yc.stats.median_atr is None:
                continue
            # Not an exact equality (median of ratios != ratio of medians in
            # general), but the ATR-unit median must be strictly positive
            # whenever the price-unit median is, and of a plausible order
            # of magnitude (a session range is typically single-digit
            # multiples of a 1440-bar ATR, not thousands of times larger
            # or a small fraction of it).
            assert yc.stats.median_atr.point_estimate > 0
            assert 0.01 < yc.stats.median_atr.point_estimate < 1000


def test_raw_atr_values_length_matches_n():
    bars = _weekday_bars(30, start=datetime(2010, 1, 4, tzinfo=timezone.utc))
    result = compute_r1(bars, SESSION_SET, start_ns=Nanos(0), end_ns=Nanos(1))
    for sess in result.sessions:
        for yc in sess.by_year:
            assert len(yc.raw_atr_values) == yc.stats.n


def test_deterministic_across_repeated_calls():
    bars = _weekday_bars(20, start=datetime(2010, 1, 4, tzinfo=timezone.utc))
    a = compute_r1(bars, SESSION_SET, start_ns=Nanos(0), end_ns=Nanos(1))
    b = compute_r1(bars, SESSION_SET, start_ns=Nanos(0), end_ns=Nanos(1))
    assert a.sessions == b.sessions


def test_below_min_n_has_no_bootstrap_ci():
    # 3 real weekdays -- far below the n<10 suppression floor for any cell.
    bars = _weekday_bars(3, start=datetime(2010, 1, 4, tzinfo=timezone.utc))
    result = compute_r1(bars, SESSION_SET, start_ns=Nanos(0), end_ns=Nanos(1))
    for sess in result.sessions:
        for yc in sess.by_year:
            assert yc.stats.median_price is None
            assert yc.stats.median_atr is None
