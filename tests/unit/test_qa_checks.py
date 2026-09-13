from datetime import date, datetime, timezone

import numpy as np
import polars as pl
import pytest

from jarvis.bars import BAR_SCHEMA
from jarvis.core.types import Nanos
from jarvis.ingest.fetch_log import FetchLogEntry
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


def _add(
    acc: TickChecksAccumulator,
    ts: list[int],
    bid: list[float],
    ask: list[float],
    bid_vol: list[float] | None = None,
    ask_vol: list[float] | None = None,
    label: str = "2024-01",
) -> None:
    """WP-010: TickChecksAccumulator.add_batch takes plain arrays plus a
    batch label (a "YYYY-MM" data/tick/ month, in real use) instead of the
    old TickArrays + hour_ns pair -- this test suite runs entirely against
    synthetic arrays, never real TickArrays, so the label is arbitrary."""
    n = len(ts)
    acc.add_batch(
        np.array(ts, dtype=np.int64),
        np.array(bid, dtype=np.float64),
        np.array(ask, dtype=np.float64),
        np.array(bid_vol if bid_vol is not None else [1.0] * n, dtype=np.float64),
        np.array(ask_vol if ask_vol is not None else [1.0] * n, dtype=np.float64),
        label,
    )


def _find(findings, check_id: str):
    return next((f for f in findings if f.check_id == check_id), None)


def _ns_ms(y: int, mo: int, d: int, h: int, mi: int, s: int, ms: int = 0) -> int:
    base = int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp()) * 1_000_000_000
    return base + ms * 1_000_000


# WP-013: E-01/W-07 reconciliation, against the REAL historical row values ---
#
# These timestamps and bid/ask values are the actual rows found by WP-013's
# full-archive scan of the real data/tick/ store (not fabricated) -- kept as
# hermetic fixtures here rather than reading the live, gitignored,
# machine-local store directly, same reasoning WP-010's tests already use.


def test_real_20091113_zero_spread_cluster_is_w07_not_e01():
    """D-055g already established these five rows (the only zero-spread
    cluster in November 2009) as genuine, not corruption, at the ingest
    layer. Before WP-013, qa/checks.py's own separate E-01 check still
    flagged them as ERROR anyway -- this is the exact finding that halted
    the real Stage 1A run at Step 2, and the runbook's own Section 5 had
    (wrongly, before this fix) said they should not appear as E-01 at all."""
    acc = TickChecksAccumulator()
    _add(
        acc,
        ts=[
            _ns_ms(2009, 11, 13, 20, 30, 8),
            _ns_ms(2009, 11, 13, 20, 30, 26),
            _ns_ms(2009, 11, 13, 20, 30, 27),
            _ns_ms(2009, 11, 13, 20, 30, 30),
            _ns_ms(2009, 11, 13, 20, 30, 31),
        ],
        bid=[1.669100, 1.668700, 1.668700, 1.668700, 1.668700],
        ask=[1.669100, 1.668700, 1.668700, 1.668700, 1.668700],
        label="2009-11",
    )
    findings = acc.finalize()
    assert _find(findings, "E-01") is None
    w07 = _find(findings, "W-07")
    assert w07 is not None
    assert w07.severity == "WARNING"
    assert w07.count == 5


def test_real_20160624_brexit_cluster_is_w07_not_e01():
    """The five zero-spread ticks from 2016-06-24 10:14:56-57 UTC --
    GBP/USD's Brexit-referendum-result crash. WP-013 confirmed directly
    against the surrounding real ticks (not assumed): ~2x the day's
    ordinary tick volume, ~8.6x the day's ordinary price range, and these
    five rows themselves sit in a smoothly monotonic price decline
    (1.379400 -> ... -> 1.379220) bracketed immediately by normal
    non-zero spreads on both sides -- consistent with momentarily
    evaporated two-sided liquidity during genuine extreme volatility, not
    a decode defect."""
    acc = TickChecksAccumulator()
    _add(
        acc,
        ts=[
            _ns_ms(2016, 6, 24, 10, 14, 56, 857),
            _ns_ms(2016, 6, 24, 10, 14, 57, 123),
            _ns_ms(2016, 6, 24, 10, 14, 57, 343),
            _ns_ms(2016, 6, 24, 10, 14, 57, 577),
            _ns_ms(2016, 6, 24, 10, 14, 57, 827),
        ],
        bid=[1.379400, 1.379330, 1.379280, 1.379240, 1.379220],
        ask=[1.379400, 1.379330, 1.379280, 1.379240, 1.379220],
        label="2016-06",
    )
    findings = acc.finalize()
    assert _find(findings, "E-01") is None
    w07 = _find(findings, "W-07")
    assert w07 is not None
    assert w07.severity == "WARNING"
    assert w07.count == 5


def test_finding_sample_is_not_capped_at_accumulation():
    """WP-013: the old _SAMPLE_LIMIT=10 cap applied inside _append_sample
    itself, so Finding.sample -- the thing both the markdown report AND
    the Parquet sidecar are built from -- silently lost anything past the
    10th match, for every check, forever. This is exactly why the real
    Stage 1A run's E-01 finding (count=11) couldn't reveal where its
    11th occurrence was: neither the markdown nor its Parquet sidecar
    ever had it. The cap now lives only in qa/report.py's markdown
    renderer; the accumulator (and therefore Finding.sample) must hold
    every match."""
    acc = TickChecksAccumulator()
    n = 25
    _add(
        acc,
        ts=list(range(0, n * 1000, 1000)),
        bid=[1.0] * n,
        ask=[1.0] * n,  # every row zero-spread -> W-07, n=25 matches
    )
    w07 = _find(acc.finalize(), "W-07")
    assert w07 is not None
    assert w07.count == 25
    assert len(w07.sample) == 25  # not capped at 10


def test_negative_spread_still_raises_e01_at_error_severity():
    """WP-013 narrows E-01 to strictly-negative spread only -- it must NOT
    also narrow it out of existence. A true inversion (ask < bid), which
    the full-archive scan found precisely zero of anywhere in 2006-2022,
    stays an ERROR if it is ever seen."""
    acc = TickChecksAccumulator()
    _add(acc, ts=[0], bid=[1.30010], ask=[1.30000])  # ask < bid
    findings = acc.finalize()
    e01 = _find(findings, "E-01")
    assert e01 is not None
    assert e01.severity == "ERROR"
    assert e01.count == 1
    assert _find(findings, "W-07") is None


# E-01 -----------------------------------------------------------------


def test_e01_and_w07_reported_as_separate_findings():
    """WP-013: negative spread (E-01, ERROR) and zero spread (W-07,
    WARNING) are no longer one combined E-01 finding -- see D-063 for why
    a real Stage 1A run made this split necessary."""
    acc = TickChecksAccumulator()
    _add(
        acc,
        ts=[0, 1000, 2000, 3000],
        bid=[1.0, 1.0005, 1.0, 1.0],
        ask=[1.0001, 1.0000, 1.0, 1.0002],  # idx1: ask<bid (negative); idx2: ask==bid (zero)
    )
    findings = acc.finalize()

    e01 = _find(findings, "E-01")
    assert e01 is not None
    assert e01.severity == "ERROR"
    assert e01.count == 1
    assert "strictly negative" in e01.detail

    w07 = _find(findings, "W-07")
    assert w07 is not None
    assert w07.severity == "WARNING"
    assert w07.count == 1
    assert "ask == bid" in w07.detail


def test_e01_no_finding_when_spread_always_positive():
    acc = TickChecksAccumulator()
    _add(acc, ts=[0, 1000], bid=[1.0, 1.0001], ask=[1.0002, 1.0003])
    findings = acc.finalize()
    assert _find(findings, "E-01") is None
    assert _find(findings, "W-07") is None


# E-02 -----------------------------------------------------------------


def test_e02_non_positive_price_detected():
    acc = TickChecksAccumulator()
    _add(acc, ts=[0, 1000], bid=[1.0, -0.5], ask=[1.0002, 1.0003])
    finding = _find(acc.finalize(), "E-02")
    assert finding is not None
    assert finding.severity == "ERROR"
    assert finding.count == 1


def test_e02_no_finding_when_prices_positive():
    acc = TickChecksAccumulator()
    _add(acc, ts=[0, 1000], bid=[1.0, 1.0001], ask=[1.0002, 1.0003])
    assert _find(acc.finalize(), "E-02") is None


# E-03 -------------------------------------------------------------------


def test_e03_reversal_detected():
    acc = TickChecksAccumulator()
    _add(acc, ts=[100, 200, 150], bid=[1.0, 1.0, 1.0], ask=[1.0001, 1.0001, 1.0001])
    finding = _find(acc.finalize(), "E-03")
    assert finding is not None
    assert finding.severity == "ERROR"
    assert finding.count == 1


def test_e03_equal_consecutive_timestamps_not_flagged():
    """Acceptance criterion 3: two ticks sharing a millisecond is not a
    reversal -- only a strict decrease is."""
    acc = TickChecksAccumulator()
    _add(acc, ts=[100, 100, 200], bid=[1.0, 1.0, 1.0], ask=[1.0001, 1.0001, 1.0001])
    assert _find(acc.finalize(), "E-03") is None


# W-01 -------------------------------------------------------------------


def test_w01_duplicate_tick_detected():
    acc = TickChecksAccumulator()
    _add(acc, ts=[100, 100, 200], bid=[1.0, 1.0, 1.0002], ask=[1.0001, 1.0001, 1.0003])
    finding = _find(acc.finalize(), "W-01")
    assert finding is not None
    assert finding.severity == "WARNING"
    assert finding.count == 1


def test_w01_no_finding_when_ticks_differ():
    acc = TickChecksAccumulator()
    _add(acc, ts=[100, 200], bid=[1.0, 1.0002], ask=[1.0001, 1.0003])
    assert _find(acc.finalize(), "W-01") is None


# W-02 -------------------------------------------------------------------


def _jump_ticks(n_stable: int, jump_size: float) -> tuple[list[int], list[float], list[float]]:
    rng = np.random.default_rng(7)
    ts = np.arange(n_stable + 2, dtype=np.int64) * 1_000_000  # 1ms apart
    mid = 1.10000 + np.cumsum(rng.uniform(-1e-5, 1e-5, size=n_stable + 1))
    mid = np.concatenate(([1.10000], mid))
    if jump_size:
        mid[-1] = mid[-2] + jump_size
    bid = mid - 0.00005
    ask = mid + 0.00005
    return ts.tolist(), bid.tolist(), ask.tolist()


def test_w02_unrealistic_jump_detected():
    ts, bid, ask = _jump_ticks(n_stable=1005, jump_size=0.05)  # far larger than the noise floor
    acc = TickChecksAccumulator()
    _add(acc, ts=ts, bid=bid, ask=ask)
    finding = _find(acc.finalize(), "W-02")
    assert finding is not None
    assert finding.severity == "WARNING"
    assert finding.count >= 1


def test_w02_no_finding_for_normal_moves():
    ts, bid, ask = _jump_ticks(n_stable=1005, jump_size=0.0)
    acc = TickChecksAccumulator()
    _add(acc, ts=ts, bid=bid, ask=ask)
    assert _find(acc.finalize(), "W-02") is None


# W-03 -------------------------------------------------------------------


def test_w03_weekend_activity_detected():
    # 2024-01-06 is a Saturday, comfortably mid-gap.
    hour_ns = _ns(2024, 1, 6, 12)
    acc = TickChecksAccumulator()
    _add(acc, ts=[hour_ns, hour_ns + NS_PER_MINUTE], bid=[1.0, 1.0001], ask=[1.0002, 1.0003])
    finding = _find(acc.finalize(), "W-03")
    assert finding is not None
    assert finding.severity == "WARNING"
    assert finding.count == 2


def test_w03_no_finding_for_weekday_ticks():
    # 2024-01-09 is a Tuesday.
    hour_ns = _ns(2024, 1, 9, 12)
    acc = TickChecksAccumulator()
    _add(acc, ts=[hour_ns, hour_ns + NS_PER_MINUTE], bid=[1.0, 1.0001], ask=[1.0002, 1.0003])
    assert _find(acc.finalize(), "W-03") is None


def test_w03_buffer_excludes_boundary_adjacent_activity():
    """A tick within 5 minutes of the gap boundary must NOT be flagged,
    even though it is technically inside is_weekend_gap."""
    # Friday 22:00 UTC (17:00 EST) is the winter gap start; 22:02 UTC is
    # inside the gap but within the 5-minute buffer.
    hour_ns = _ns(2024, 1, 5, 22)  # 2024-01-05 is a Friday
    boundary_adjacent = hour_ns + 2 * NS_PER_MINUTE
    acc = TickChecksAccumulator()
    _add(acc, ts=[boundary_adjacent], bid=[1.0], ask=[1.0002])
    assert _find(acc.finalize(), "W-03") is None


# I-02 -------------------------------------------------------------------


def test_i02_volume_all_zero_detected():
    acc = TickChecksAccumulator()
    _add(
        acc,
        ts=[0, 1000],
        bid=[1.0, 1.0001],
        ask=[1.0002, 1.0003],
        bid_vol=[0.0, 0.0],
        ask_vol=[0.0, 0.0],
    )
    finding = _find(acc.finalize(), "I-02")
    assert finding is not None
    assert finding.severity == "INFO"
    assert finding.count == 1


def test_i02_no_finding_when_volume_present():
    acc = TickChecksAccumulator()
    _add(
        acc,
        ts=[0, 1000],
        bid=[1.0, 1.0001],
        ask=[1.0002, 1.0003],
        bid_vol=[1.0, 0.0],
        ask_vol=[0.0, 0.0],
    )
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


# I-01 -- WP-014 redesign: flags LEAKED (unexpected nonzero) bar activity
# on a DST-transition trading day's own (market-closed) label, rather than
# comparing against a full-day expectation that label can never meet.
# ---------------------------------------------------------------------------


def _dst_transition_days(start: date, count: int) -> list[date]:
    """Real DST-transition trading-day labels, found the same way
    _check_dst_day itself finds them (trading_day_bounds' own computed
    span != 1440 minutes) -- not a hardcoded date list that could drift
    from the actual historical US rules."""
    from datetime import timedelta

    days: list[date] = []
    d = start
    while len(days) < count:
        s, e = trading_day_bounds(d)
        if (e - s) // NS_PER_MINUTE != 1440:
            days.append(d)
        d = d + timedelta(days=1)
    return days


def test_i01_confirms_clean_when_dst_sunday_has_zero_bars():
    """The real, expected case (confirmed against the actual archive in
    WP-014: 0 of 33 flagged) -- a DST-transition Sunday with no bars on
    its own label. I-01 must still appear (positive confirmation, not
    silence) with count=0."""
    dst_day = date(2023, 3, 12)  # US spring-forward Sunday, a 23h label
    s, e = trading_day_bounds(dst_day)
    assert (e - s) // NS_PER_MINUTE == 23 * 60

    # Bars exist on an ordinary surrounding day, never on the DST Sunday
    # label itself.
    normal_day = date(2023, 3, 8)
    ns, ne = trading_day_bounds(normal_day)
    rows = [_bar_row(ns + m * NS_PER_MINUTE) for m in range(50)]

    bars = _bars_frame(rows)
    findings = bar_level_checks(bars, ns, e, thin_day_threshold=0.60)

    i01 = _find(findings, "I-01")
    assert i01 is not None
    assert i01.severity == "INFO"
    assert i01.count == 0
    assert i01.sample == ()
    assert "0 of" in i01.detail
    assert "confirms" in i01.detail


def test_i01_flags_leaked_bars_on_dst_sunday():
    """WP-014's required proof that the redesign actually catches the
    failure mode it exists for: if trading_day_bounds or the day-
    bucketing search ever mis-locates a DST-adjusted boundary, real bars
    leak onto the closed Sunday label -- inject exactly that and confirm
    I-01 flags it specifically, not the ordinary day beside it."""
    dst_day = date(2023, 11, 5)  # US fall-back Sunday, a 25h label
    s, e = trading_day_bounds(dst_day)
    assert (e - s) // NS_PER_MINUTE == 25 * 60

    # Simulates a boundary-leakage bug: three bars land inside the
    # Sunday-labelled bounds, which should be entirely bar-free.
    leaked_rows = [_bar_row(s + m * NS_PER_MINUTE) for m in range(3)]

    normal_day = date(2023, 11, 1)  # an ordinary Wednesday, populated normally
    ns, _ne = trading_day_bounds(normal_day)
    normal_rows = [_bar_row(ns + m * NS_PER_MINUTE) for m in range(50)]

    bars = _bars_frame(leaked_rows + normal_rows)
    findings = bar_level_checks(bars, ns, e, thin_day_threshold=0.60)

    i01 = _find(findings, "I-01")
    assert i01 is not None
    assert i01.severity == "INFO"
    assert i01.count == 1
    assert dst_day.isoformat() in i01.sample[0]
    assert "actual_bars=3" in i01.sample[0]
    assert normal_day.isoformat() not in " ".join(i01.sample)


def test_i01_count_not_capped_past_ten():
    """WP-014: the sibling of WP-013's E-01/count-cap bug -- I-01's count
    used to be len() of the very list its samples were capped into.
    Confirms the fix against 12 real DST-transition days (more than the
    old 10-sample cap), all leaked, all counted."""
    days = _dst_transition_days(date(2007, 1, 1), 12)
    assert len(days) == 12

    rows = []
    for d in days:
        s, _e = trading_day_bounds(d)
        rows.append(_bar_row(s))  # one leaked bar on each DST Sunday label

    start_ns, _ = trading_day_bounds(days[0])
    _, end_ns = trading_day_bounds(days[-1])
    bars = _bars_frame(rows)
    findings = bar_level_checks(bars, start_ns, end_ns, thin_day_threshold=0.60)

    i01 = _find(findings, "I-01")
    assert i01 is not None
    assert i01.count == 12  # not capped at 10
    assert len(i01.sample) == 12


def test_bar_level_checks_empty_frame_returns_no_findings():
    bars = _bars_frame([])
    findings = bar_level_checks(bars, _ns(2024, 1, 1), _ns(2024, 1, 2), thin_day_threshold=0.60)
    assert findings == []
