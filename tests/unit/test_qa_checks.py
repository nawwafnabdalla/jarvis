from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from jarvis.bars import BAR_SCHEMA
from jarvis.core.types import Nanos
from jarvis.ingest.fetch_log import FetchLogEntry
from jarvis.ingest.parse import TickArrays
from jarvis.qa.checks import (
    FetchLogChecksAccumulator,
    TickChecksAccumulator,
    bar_level_checks,
)
from jarvis.timeengine import trading_day_bounds

NS_PER_MINUTE = 60_000_000_000
NS_PER_HOUR = 3_600_000_000_000


def _ns(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> Nanos:
    return Nanos(int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp()) * 1_000_000_000)


def _ticks(
    ts: list[int],
    bid: list[float],
    ask: list[float],
    bid_vol: list[float] | None = None,
    ask_vol: list[float] | None = None,
) -> TickArrays:
    n = len(ts)
    return TickArrays(
        instrument="GBPUSD",
        hour_utc_ns=Nanos(0),
        ts_utc_ns=np.array(ts, dtype=np.int64),
        bid=np.array(bid, dtype=np.float64),
        ask=np.array(ask, dtype=np.float64),
        bid_volume=np.array(bid_vol if bid_vol is not None else [1.0] * n, dtype=np.float64),
        ask_volume=np.array(ask_vol if ask_vol is not None else [1.0] * n, dtype=np.float64),
        record_count=n,
        source_path=Path("dummy.bi5"),
    )


def _find(findings, check_id: str):
    return next((f for f in findings if f.check_id == check_id), None)


# E-01 -----------------------------------------------------------------


def test_e01_reports_negative_and_zero_spread_separately():
    ticks = _ticks(
        ts=[0, 1000, 2000, 3000],
        bid=[1.0, 1.0005, 1.0, 1.0],
        ask=[1.0001, 1.0000, 1.0, 1.0002],  # idx1: ask<bid (negative); idx2: ask==bid (zero)
    )
    acc = TickChecksAccumulator()
    acc.add_hour(ticks, Nanos(0))
    finding = _find(acc.finalize(), "E-01")
    assert finding is not None
    assert finding.severity == "ERROR"
    assert finding.count == 2
    assert "1 strictly negative" in finding.detail
    assert "1 exactly zero" in finding.detail


def test_e01_no_finding_when_spread_always_positive():
    ticks = _ticks(ts=[0, 1000], bid=[1.0, 1.0001], ask=[1.0002, 1.0003])
    acc = TickChecksAccumulator()
    acc.add_hour(ticks, Nanos(0))
    assert _find(acc.finalize(), "E-01") is None


# E-02 -----------------------------------------------------------------


def test_e02_non_positive_price_detected():
    ticks = _ticks(ts=[0, 1000], bid=[1.0, -0.5], ask=[1.0002, 1.0003])
    acc = TickChecksAccumulator()
    acc.add_hour(ticks, Nanos(0))
    finding = _find(acc.finalize(), "E-02")
    assert finding is not None
    assert finding.severity == "ERROR"
    assert finding.count == 1


def test_e02_no_finding_when_prices_positive():
    ticks = _ticks(ts=[0, 1000], bid=[1.0, 1.0001], ask=[1.0002, 1.0003])
    acc = TickChecksAccumulator()
    acc.add_hour(ticks, Nanos(0))
    assert _find(acc.finalize(), "E-02") is None


# E-03 -------------------------------------------------------------------


def test_e03_reversal_detected():
    ticks = _ticks(ts=[100, 200, 150], bid=[1.0, 1.0, 1.0], ask=[1.0001, 1.0001, 1.0001])
    acc = TickChecksAccumulator()
    acc.add_hour(ticks, Nanos(0))
    finding = _find(acc.finalize(), "E-03")
    assert finding is not None
    assert finding.severity == "ERROR"
    assert finding.count == 1


def test_e03_equal_consecutive_timestamps_not_flagged():
    """Acceptance criterion 3: two ticks sharing a millisecond is not a
    reversal -- only a strict decrease is."""
    ticks = _ticks(ts=[100, 100, 200], bid=[1.0, 1.0, 1.0], ask=[1.0001, 1.0001, 1.0001])
    acc = TickChecksAccumulator()
    acc.add_hour(ticks, Nanos(0))
    assert _find(acc.finalize(), "E-03") is None


# W-01 -------------------------------------------------------------------


def test_w01_duplicate_tick_detected():
    ticks = _ticks(ts=[100, 100, 200], bid=[1.0, 1.0, 1.0002], ask=[1.0001, 1.0001, 1.0003])
    acc = TickChecksAccumulator()
    acc.add_hour(ticks, Nanos(0))
    finding = _find(acc.finalize(), "W-01")
    assert finding is not None
    assert finding.severity == "WARNING"
    assert finding.count == 1


def test_w01_no_finding_when_ticks_differ():
    ticks = _ticks(ts=[100, 200], bid=[1.0, 1.0002], ask=[1.0001, 1.0003])
    acc = TickChecksAccumulator()
    acc.add_hour(ticks, Nanos(0))
    assert _find(acc.finalize(), "W-01") is None


# W-02 -------------------------------------------------------------------


def _jump_ticks(n_stable: int, jump_size: float) -> TickArrays:
    rng = np.random.default_rng(7)
    ts = np.arange(n_stable + 2, dtype=np.int64) * 1_000_000  # 1ms apart
    mid = 1.10000 + np.cumsum(rng.uniform(-1e-5, 1e-5, size=n_stable + 1))
    mid = np.concatenate(([1.10000], mid))
    if jump_size:
        mid[-1] = mid[-2] + jump_size
    bid = mid - 0.00005
    ask = mid + 0.00005
    return _ticks(ts=ts.tolist(), bid=bid.tolist(), ask=ask.tolist())


def test_w02_unrealistic_jump_detected():
    ticks = _jump_ticks(n_stable=1005, jump_size=0.05)  # far larger than the noise floor
    acc = TickChecksAccumulator()
    acc.add_hour(ticks, Nanos(0))
    finding = _find(acc.finalize(), "W-02")
    assert finding is not None
    assert finding.severity == "WARNING"
    assert finding.count >= 1


def test_w02_no_finding_for_normal_moves():
    ticks = _jump_ticks(n_stable=1005, jump_size=0.0)
    acc = TickChecksAccumulator()
    acc.add_hour(ticks, Nanos(0))
    assert _find(acc.finalize(), "W-02") is None


# W-03 -------------------------------------------------------------------


def test_w03_weekend_activity_detected():
    # 2024-01-06 is a Saturday, comfortably mid-gap.
    hour_ns = _ns(2024, 1, 6, 12)
    ticks = _ticks(ts=[hour_ns, hour_ns + NS_PER_MINUTE], bid=[1.0, 1.0001], ask=[1.0002, 1.0003])
    acc = TickChecksAccumulator()
    acc.add_hour(ticks, hour_ns)
    finding = _find(acc.finalize(), "W-03")
    assert finding is not None
    assert finding.severity == "WARNING"
    assert finding.count == 2


def test_w03_no_finding_for_weekday_ticks():
    # 2024-01-09 is a Tuesday.
    hour_ns = _ns(2024, 1, 9, 12)
    ticks = _ticks(ts=[hour_ns, hour_ns + NS_PER_MINUTE], bid=[1.0, 1.0001], ask=[1.0002, 1.0003])
    acc = TickChecksAccumulator()
    acc.add_hour(ticks, hour_ns)
    assert _find(acc.finalize(), "W-03") is None


def test_w03_buffer_excludes_boundary_adjacent_activity():
    """A tick within 5 minutes of the gap boundary must NOT be flagged,
    even though it is technically inside is_weekend_gap."""
    # Friday 22:00 UTC (17:00 EST) is the winter gap start; 22:02 UTC is
    # inside the gap but within the 5-minute buffer.
    hour_ns = _ns(2024, 1, 5, 22)  # 2024-01-05 is a Friday
    boundary_adjacent = hour_ns + 2 * NS_PER_MINUTE
    ticks = _ticks(ts=[boundary_adjacent], bid=[1.0], ask=[1.0002])
    acc = TickChecksAccumulator()
    acc.add_hour(ticks, hour_ns)
    assert _find(acc.finalize(), "W-03") is None


# I-02 -------------------------------------------------------------------


def test_i02_volume_all_zero_detected():
    ticks = _ticks(
        ts=[0, 1000], bid=[1.0, 1.0001], ask=[1.0002, 1.0003], bid_vol=[0.0, 0.0], ask_vol=[0.0, 0.0]
    )
    acc = TickChecksAccumulator()
    acc.add_hour(ticks, Nanos(0))
    finding = _find(acc.finalize(), "I-02")
    assert finding is not None
    assert finding.severity == "INFO"
    assert finding.count == 1


def test_i02_no_finding_when_volume_present():
    ticks = _ticks(
        ts=[0, 1000], bid=[1.0, 1.0001], ask=[1.0002, 1.0003], bid_vol=[1.0, 0.0], ask_vol=[0.0, 0.0]
    )
    acc = TickChecksAccumulator()
    acc.add_hour(ticks, Nanos(0))
    assert _find(acc.finalize(), "I-02") is None


# E-04 / W-06 --------------------------------------------------------------


def _weekday_hours(start: Nanos, n: int) -> list[Nanos]:
    """n consecutive hours starting at `start`, assumed to already be a
    stretch with no weekend gap inside it (caller's responsibility)."""
    return [Nanos(start + i * NS_PER_HOUR) for i in range(n)]


def test_e04_missing_hours_exceed_threshold():
    # 2024-01-08 00:00 UTC is a Monday; 100 consecutive weekday hours.
    hours = _weekday_hours(_ns(2024, 1, 8, 0), 100)
    acc = FetchLogChecksAccumulator()
    for i, h in enumerate(hours):
        acc.observe_hour(h, blob_exists=(i != 0), blob_size=100, log_entry=None)
    finding = _find(acc.finalize(), "E-04")
    assert finding is not None
    assert finding.severity == "ERROR"
    assert finding.count == 1
    assert finding.year == 2024


def test_w06_missing_hours_below_threshold():
    hours = _weekday_hours(_ns(2024, 1, 8, 0), 1000)
    acc = FetchLogChecksAccumulator()
    for i, h in enumerate(hours):
        acc.observe_hour(h, blob_exists=(i != 0), blob_size=100, log_entry=None)
    finding = _find(acc.finalize(), "W-06")
    assert finding is not None
    assert finding.severity == "WARNING"
    assert finding.count == 1


def test_missing_hour_denominator_reflects_only_observed_hours():
    """Acceptance criterion 5: the denominator must not be a hardcoded
    full-year figure -- it is exactly the hours actually observed (a
    partial-year range, as D-036 requires for 2006). 1 missing of 48 is
    ~2.1%, above the 0.5% threshold, so this is E-04 (ERROR); the point of
    the test is the "48" denominator, not the severity."""
    hours = _weekday_hours(_ns(2006, 12, 4, 0), 48)  # 2006-12-04 is a Monday
    acc = FetchLogChecksAccumulator()
    for i, h in enumerate(hours):
        acc.observe_hour(h, blob_exists=(i != 0), blob_size=100, log_entry=None)
    finding = _find(acc.finalize(), "E-04")
    assert finding is not None
    assert finding.year == 2006
    assert "of 48 " in finding.detail  # not "of 8760" or any full-year figure


# E-05 -------------------------------------------------------------------


def test_e05_malformed_blob_reported():
    acc = FetchLogChecksAccumulator()
    acc.record_malformed(Nanos(0), "decompressed length 25 is not a multiple of 20")
    finding = _find(acc.finalize(), "E-05")
    assert finding is not None
    assert finding.severity == "ERROR"
    assert finding.count == 1


def test_e05_no_finding_when_nothing_malformed():
    acc = FetchLogChecksAccumulator()
    assert _find(acc.finalize(), "E-05") is None


# E-06 -------------------------------------------------------------------


def test_e06_fires_in_both_directions():
    """Acceptance criterion 6."""
    hour_a = _ns(2024, 1, 9, 3)
    hour_b = _ns(2024, 1, 9, 4)
    acc = FetchLogChecksAccumulator()
    fetched_entry = FetchLogEntry(
        hour_utc_ns=hour_a,
        status="fetched",
        attempts=1,
        byte_count=1000,
        recorded_utc="2024-01-09T03:00:00.000Z",
        error=None,
    )
    acc.observe_hour(hour_a, blob_exists=False, blob_size=0, log_entry=fetched_entry)
    acc.observe_hour(hour_b, blob_exists=True, blob_size=1000, log_entry=None)

    finding = _find(acc.finalize(), "E-06")
    assert finding is not None
    assert finding.severity == "ERROR"
    assert finding.count == 2
    assert "1 hours where the fetch log says 'fetched'" in finding.detail
    assert "1 hours with a non-empty blob and no fetch log entry" in finding.detail


def test_e06_no_finding_when_consistent():
    hour_a = _ns(2024, 1, 9, 3)
    hour_b = _ns(2024, 1, 9, 4)
    acc = FetchLogChecksAccumulator()
    fetched_entry = FetchLogEntry(
        hour_utc_ns=hour_a,
        status="fetched",
        attempts=1,
        byte_count=1000,
        recorded_utc="2024-01-09T03:00:00.000Z",
        error=None,
    )
    acc.observe_hour(hour_a, blob_exists=True, blob_size=1000, log_entry=fetched_entry)
    acc.observe_hour(hour_b, blob_exists=False, blob_size=0, log_entry=None)
    assert _find(acc.finalize(), "E-06") is None


# Bar-level: W-04, I-03, W-05, I-04, I-01 -----------------------------------


def _bar_row(ts_ns: int, spread_twa: float = 0.0001, prev_gap_ns: int | None = None) -> dict:
    return {
        "ts_utc_ns": ts_ns,
        "bid_o": 1.0,
        "bid_h": 1.0,
        "bid_l": 1.0,
        "bid_c": 1.0,
        "ask_o": 1.0 + spread_twa,
        "ask_h": 1.0 + spread_twa,
        "ask_l": 1.0 + spread_twa,
        "ask_c": 1.0 + spread_twa,
        "tick_count": 1,
        "first_tick_ns": ts_ns,
        "last_tick_ns": ts_ns,
        "spread_open": spread_twa,
        "spread_max": spread_twa,
        "spread_twa": spread_twa,
        "prev_gap_ns": prev_gap_ns,
    }


def _bars_frame(rows: list[dict]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema=BAR_SCHEMA)
    return pl.DataFrame(rows, schema=BAR_SCHEMA)


def test_w04_extreme_spread_detected_and_small_bucket_reported_as_i03():
    # Two Mondays in January 2024 (no DST, London == UTC), 10:00-10:59
    # each: 60 bars/day x 2 = 120 bars in the (Monday, 10) bucket -- above
    # the 100-bar minimum, so the check runs for that bucket.
    rows = []
    for day_start in (_ns(2024, 1, 8, 10), _ns(2024, 1, 15, 10)):
        for m in range(60):
            rows.append(_bar_row(day_start + m * NS_PER_MINUTE))
    # One extreme outlier well over 20x the ~0.0001 median, same (weekday,
    # hour) bucket as the 120 normal bars above (duplicate ts is fine here
    # -- bar_level_checks buckets and compares rows independently, it does
    # not assume ts_utc_ns is unique within a bucket).
    rows.append(_bar_row(_ns(2024, 1, 8, 10) + 30 * NS_PER_MINUTE, spread_twa=0.01))

    # A second, tiny bucket (Tuesday 10:00) with only a handful of bars --
    # must be skipped and reported as I-03, not evaluated by W-04.
    for m in range(5):
        rows.append(_bar_row(_ns(2024, 1, 9, 10) + m * NS_PER_MINUTE))

    bars = _bars_frame(rows)
    start_ns = _ns(2024, 1, 8, 0)
    end_ns = _ns(2024, 1, 16, 0)
    findings = bar_level_checks(bars, start_ns, end_ns, thin_day_threshold=0.60)

    w04 = _find(findings, "W-04")
    assert w04 is not None
    assert w04.severity == "WARNING"
    assert w04.count == 1

    i03 = _find(findings, "I-03")
    assert i03 is not None
    assert i03.severity == "INFO"
    assert i03.count >= 1


def test_w04_no_finding_when_spreads_are_uniform():
    rows = []
    for day_start in (_ns(2024, 1, 8, 10), _ns(2024, 1, 15, 10)):
        for m in range(60):
            rows.append(_bar_row(day_start + m * NS_PER_MINUTE))
    bars = _bars_frame(rows)
    start_ns = _ns(2024, 1, 8, 0)
    end_ns = _ns(2024, 1, 16, 0)
    findings = bar_level_checks(bars, start_ns, end_ns, thin_day_threshold=0.60)
    assert _find(findings, "W-04") is None


def _weekday_dates_from(start: date, n: int) -> list[date]:
    from datetime import timedelta

    days = []
    d = start
    while len(days) < n:
        if d.isoweekday() <= 5:
            days.append(d)
        d = d + timedelta(days=1)
    return days


def test_w05_thin_day_detected_after_sufficient_baseline():
    days = _weekday_dates_from(date(2024, 1, 1), 21)  # 20 baseline + 1 test day
    rows = []
    for d in days[:20]:
        s, _e = trading_day_bounds(d)
        for m in range(100):
            rows.append(_bar_row(s + m * NS_PER_MINUTE))
    thin_day = days[20]
    s, _e = trading_day_bounds(thin_day)
    for m in range(10):  # well below 60% of the 100-bar baseline median
        rows.append(_bar_row(s + m * NS_PER_MINUTE))

    bars = _bars_frame(rows)
    start_ns, _ = trading_day_bounds(days[0])
    _, end_ns = trading_day_bounds(days[-1])
    findings = bar_level_checks(bars, start_ns, end_ns, thin_day_threshold=0.60)

    w05 = _find(findings, "W-05")
    assert w05 is not None
    assert w05.severity == "WARNING"
    assert w05.count == 1

    i04 = _find(findings, "I-04")
    assert i04 is not None
    assert i04.severity == "INFO"
    assert i04.count == 20  # the first 20 days have no 20-day baseline yet


def test_w05_no_finding_when_all_days_normal():
    days = _weekday_dates_from(date(2024, 1, 1), 21)
    rows = []
    for d in days:
        s, _e = trading_day_bounds(d)
        for m in range(100):
            rows.append(_bar_row(s + m * NS_PER_MINUTE))
    bars = _bars_frame(rows)
    start_ns, _ = trading_day_bounds(days[0])
    _, end_ns = trading_day_bounds(days[-1])
    findings = bar_level_checks(bars, start_ns, end_ns, thin_day_threshold=0.60)
    assert _find(findings, "W-05") is None


# I-01 ---------------------------------------------------------------------


def test_i01_fires_on_dst_transition_day_with_wrong_bar_count():
    # 2023-03-12 is the US spring-forward Sunday: a 23-hour trading day.
    dst_day = date(2023, 3, 12)
    s, e = trading_day_bounds(dst_day)
    expected_minutes = (e - s) // NS_PER_MINUTE
    assert expected_minutes == 23 * 60

    rows = [_bar_row(s + m * NS_PER_MINUTE) for m in range(100)]  # deliberately far off

    normal_day = date(2023, 3, 8)  # an ordinary Wednesday, also given a "wrong" count
    ns, ne = trading_day_bounds(normal_day)
    rows += [_bar_row(ns + m * NS_PER_MINUTE) for m in range(50)]

    bars = _bars_frame(rows)
    findings = bar_level_checks(bars, ns, e, thin_day_threshold=0.60)

    i01 = _find(findings, "I-01")
    assert i01 is not None
    assert i01.severity == "INFO"
    assert i01.count == 1
    assert dst_day.isoformat() in i01.sample[0]
    assert normal_day.isoformat() not in " ".join(i01.sample)


def test_i01_does_not_fire_on_correctly_populated_dst_day():
    dst_day = date(2023, 11, 5)  # US fall-back Sunday: a 25-hour trading day
    s, e = trading_day_bounds(dst_day)
    expected_minutes = (e - s) // NS_PER_MINUTE
    assert expected_minutes == 25 * 60

    rows = [_bar_row(s + m * NS_PER_MINUTE) for m in range(expected_minutes)]
    bars = _bars_frame(rows)
    findings = bar_level_checks(bars, s, e, thin_day_threshold=0.60)
    assert _find(findings, "I-01") is None


def test_bar_level_checks_empty_frame_returns_no_findings():
    bars = _bars_frame([])
    findings = bar_level_checks(bars, _ns(2024, 1, 1), _ns(2024, 1, 2), thin_day_threshold=0.60)
    assert findings == []
