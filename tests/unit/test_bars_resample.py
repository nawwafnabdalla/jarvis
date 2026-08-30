import lzma
import struct
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest

from jarvis.core.errors import IntegrityError, UserError
from jarvis.core.types import Nanos
from jarvis.bars.resample import NS_PER_MINUTE, resample_range
from jarvis.bars.store import read_bars
from jarvis.ingest.urls import NS_PER_HOUR, raw_blob_path

_RECORD_STRUCT = struct.Struct(">IIIff")
_POINT_SCALE = 1.0e-5


def _hour_ns(y: int, mo: int, d: int, h: int) -> Nanos:
    return Nanos(int(datetime(y, mo, d, h, tzinfo=timezone.utc).timestamp()) * 1_000_000_000)


def _write_blob(repo_root: Path, instrument: str, hour_ns: Nanos, records: list[tuple]) -> None:
    """records: (ms, ask_points, bid_points, ask_vol, bid_vol) tuples."""
    raw = b"".join(_RECORD_STRUCT.pack(*r) for r in records)
    path = raw_blob_path(repo_root, instrument, hour_ns)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(lzma.compress(raw))


def _write_empty_blob(repo_root: Path, instrument: str, hour_ns: Nanos) -> None:
    path = raw_blob_path(repo_root, instrument, hour_ns)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")


@pytest.fixture
def repo(isolated_repo: Path) -> Path:
    (isolated_repo / "config" / "instruments.yaml").write_text(
        "GBPUSD:\n  point_scale: 1.0e-5\n  digits: 5\n", encoding="utf-8"
    )
    return isolated_repo


# Acceptance 2: absent-minute semantics -----------------------------------


def test_absent_minutes_produce_no_rows(repo: Path):
    hour = _hour_ns(2024, 1, 15, 3)
    # Ticks only in minute 0 and minute 5; minutes 1-4 must be entirely absent.
    _write_blob(
        repo,
        "GBPUSD",
        hour,
        [
            (0, 100000, 99900, 1.0, 1.0),
            (5 * 60_000, 100010, 99910, 1.0, 1.0),
        ],
    )

    report = resample_range(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))
    assert report.bars_written == 2

    df = read_bars(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))
    ts_values = sorted(df["ts_utc_ns"].to_list())
    assert ts_values == [hour, hour + 5 * NS_PER_MINUTE]

    # Explicit absence check, not just a row count coincidence.
    for missing_minute in range(1, 5):
        absent_ts = hour + missing_minute * NS_PER_MINUTE
        assert absent_ts not in ts_values


def test_no_ticks_at_all_produces_zero_bars(repo: Path):
    hour = _hour_ns(2024, 1, 15, 3)
    _write_empty_blob(repo, "GBPUSD", hour)

    report = resample_range(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))
    assert report.bars_written == 0
    assert report.hours_empty == 1
    assert report.hours_unfetched == 0


# Acceptance 3: bid_h/ask_h from different ticks ---------------------------


def test_bid_high_and_ask_high_sourced_from_different_ticks(repo: Path):
    hour = _hour_ns(2024, 1, 15, 3)
    # Tick A: high bid, low ask. Tick B: low bid, high ask.
    # The naive-wrong implementation (derive both sides from a mid price)
    # would not be able to produce this combination at all.
    _write_blob(
        repo,
        "GBPUSD",
        hour,
        [
            (0, 100000, 99990, 1.0, 1.0),  # ask=1.00000, bid=0.99990 (bid high)
            (1_000, 100050, 99900, 1.0, 1.0),  # ask=1.00050, bid=0.99900 (ask high)
        ],
    )

    report = resample_range(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))
    assert report.bars_written == 1

    df = read_bars(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))
    row = df.row(0, named=True)
    assert row["bid_h"] == pytest.approx(0.99990, abs=1e-9)  # from tick A
    assert row["ask_h"] == pytest.approx(1.00050, abs=1e-9)  # from tick B
    # If bid_h and ask_h had come from the same tick, ask_h - bid_h would
    # equal that tick's own spread (0.00010 or 0.00090) -- it does not.
    assert (row["ask_h"] - row["bid_h"]) not in (
        pytest.approx(0.00010, abs=1e-9),
        pytest.approx(0.00090, abs=1e-9),
    )


# Acceptance 4: spread_twa ---------------------------------------------


def test_spread_twa_hand_verified(repo: Path):
    hour = _hour_ns(2024, 1, 15, 3)
    # Ticks at 10s, 30s, 45s within the minute. Spreads: 0.00010, 0.00020, 0.00010.
    _write_blob(
        repo,
        "GBPUSD",
        hour,
        [
            (10_000, 100010, 100000, 1.0, 1.0),  # spread 0.00010
            (30_000, 100030, 100010, 1.0, 1.0),  # spread 0.00020
            (45_000, 100040, 100030, 1.0, 1.0),  # spread 0.00010
        ],
    )

    report = resample_range(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))
    assert report.bars_written == 1

    df = read_bars(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))
    row = df.row(0, named=True)

    # weights: (30-10)=20s, (45-30)=15s, (60-45)=15s; total = 50s = (60-10)s.
    expected_twa = (0.00010 * 20 + 0.00020 * 15 + 0.00010 * 15) / 50
    assert row["spread_twa"] == pytest.approx(expected_twa, abs=1e-9)
    assert row["spread_open"] == pytest.approx(0.00010, abs=1e-9)
    assert row["spread_max"] == pytest.approx(0.00020, abs=1e-9)


def test_spread_twa_single_tick_bar_equals_open_equals_max(repo: Path):
    hour = _hour_ns(2024, 1, 15, 3)
    _write_blob(repo, "GBPUSD", hour, [(10_000, 100025, 100000, 1.0, 1.0)])

    report = resample_range(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))
    assert report.bars_written == 1

    df = read_bars(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))
    row = df.row(0, named=True)
    assert row["spread_twa"] == pytest.approx(row["spread_open"], abs=1e-12)
    assert row["spread_twa"] == pytest.approx(row["spread_max"], abs=1e-12)
    assert row["spread_open"] == pytest.approx(0.00025, abs=1e-9)


# Acceptance 5: prev_gap_ns -----------------------------------------------


def test_prev_gap_ns_null_for_first_bar_of_run(repo: Path):
    hour = _hour_ns(2024, 1, 15, 3)
    _write_blob(repo, "GBPUSD", hour, [(0, 100000, 99900, 1.0, 1.0)])

    resample_range(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))
    df = read_bars(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))
    assert df.row(0, named=True)["prev_gap_ns"] is None


def test_prev_gap_ns_correct_across_hour_boundary(repo: Path):
    hour1 = _hour_ns(2024, 1, 15, 3)
    hour2 = Nanos(hour1 + NS_PER_HOUR)

    # Last tick of hour1 at ms=3_599_000 (59:59 into the hour).
    _write_blob(repo, "GBPUSD", hour1, [(3_599_000, 100000, 99900, 1.0, 1.0)])
    # First tick of hour2 at ms=500 into the hour.
    _write_blob(repo, "GBPUSD", hour2, [(500, 100010, 99910, 1.0, 1.0)])

    resample_range(repo, "GBPUSD", hour1, Nanos(hour2 + NS_PER_HOUR))
    df = read_bars(repo, "GBPUSD", hour1, Nanos(hour2 + NS_PER_HOUR)).sort("ts_utc_ns")

    assert df.height == 2
    first_row, second_row = df.row(0, named=True), df.row(1, named=True)
    assert first_row["prev_gap_ns"] is None

    last_tick_of_first = hour1 + 3_599_000 * 1_000_000
    first_tick_of_second = hour2 + 500 * 1_000_000
    assert second_row["prev_gap_ns"] == first_tick_of_second - last_tick_of_first


def test_prev_gap_ns_carries_across_a_market_closed_hour(repo: Path):
    hour1 = _hour_ns(2024, 1, 15, 3)
    hour2 = Nanos(hour1 + NS_PER_HOUR)  # market closed, 0-byte blob
    hour3 = Nanos(hour2 + NS_PER_HOUR)

    _write_blob(repo, "GBPUSD", hour1, [(0, 100000, 99900, 1.0, 1.0)])
    _write_empty_blob(repo, "GBPUSD", hour2)
    _write_blob(repo, "GBPUSD", hour3, [(0, 100010, 99910, 1.0, 1.0)])

    resample_range(repo, "GBPUSD", hour1, Nanos(hour3 + NS_PER_HOUR))
    df = read_bars(repo, "GBPUSD", hour1, Nanos(hour3 + NS_PER_HOUR)).sort("ts_utc_ns")

    assert df.height == 2
    gap = df.row(1, named=True)["prev_gap_ns"]
    assert gap == hour3 - hour1  # gap spans the whole closed hour, not reset by it


# Item 4: hole detection ---------------------------------------------------


def test_hole_raises_by_default(repo: Path):
    hour1 = _hour_ns(2024, 1, 15, 3)
    hour2 = Nanos(hour1 + NS_PER_HOUR)
    _write_blob(repo, "GBPUSD", hour1, [(0, 100000, 99900, 1.0, 1.0)])
    # hour2: no blob at all -- a hole.

    with pytest.raises(IntegrityError):
        resample_range(repo, "GBPUSD", hour1, Nanos(hour2 + NS_PER_HOUR))


def test_hole_proceeds_under_allow_incomplete(repo: Path):
    hour1 = _hour_ns(2024, 1, 15, 3)
    hour2 = Nanos(hour1 + NS_PER_HOUR)
    _write_blob(repo, "GBPUSD", hour1, [(0, 100000, 99900, 1.0, 1.0)])

    report = resample_range(
        repo, "GBPUSD", hour1, Nanos(hour2 + NS_PER_HOUR), allow_incomplete=True
    )
    assert report.hours_unfetched == 1
    assert report.unfetched_hours == (hour2,)
    assert report.bars_written == 1  # only hour1 resampled


def test_market_closed_hour_is_not_a_hole(repo: Path):
    hour1 = _hour_ns(2024, 1, 15, 3)
    hour2 = Nanos(hour1 + NS_PER_HOUR)
    _write_blob(repo, "GBPUSD", hour1, [(0, 100000, 99900, 1.0, 1.0)])
    _write_empty_blob(repo, "GBPUSD", hour2)

    # Must NOT raise: a 0-byte blob is a legitimate zero-bar hour.
    report = resample_range(repo, "GBPUSD", hour1, Nanos(hour2 + NS_PER_HOUR))
    assert report.hours_unfetched == 0
    assert report.hours_empty == 1
    assert report.bars_written == 1


def test_allow_incomplete_does_not_default_to_true(repo: Path):
    import inspect

    sig = inspect.signature(resample_range)
    assert sig.parameters["allow_incomplete"].default is False


# Open/close tick order ----------------------------------------------------


def test_open_and_close_use_file_order_not_numeric_sort(repo: Path):
    hour = _hour_ns(2024, 1, 15, 3)
    # Same millisecond, tie-broken by seq (file order): first record is the
    # "open" tick even though its ask is numerically larger.
    _write_blob(
        repo,
        "GBPUSD",
        hour,
        [
            (0, 100050, 99950, 1.0, 1.0),  # seq 0 -> open
            (0, 100010, 99910, 1.0, 1.0),  # seq 1 -> close
        ],
    )

    resample_range(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))
    df = read_bars(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))
    row = df.row(0, named=True)
    assert row["ask_o"] == pytest.approx(1.00050, abs=1e-9)
    assert row["ask_c"] == pytest.approx(1.00010, abs=1e-9)


# Validation ----------------------------------------------------------------


def test_non_hour_aligned_start_raises_user_error(repo: Path):
    hour = _hour_ns(2024, 1, 15, 3)
    with pytest.raises(UserError):
        resample_range(repo, "GBPUSD", Nanos(hour + 1), Nanos(hour + NS_PER_HOUR))


def test_end_before_start_raises_user_error(repo: Path):
    hour = _hour_ns(2024, 1, 15, 3)
    with pytest.raises(UserError):
        resample_range(repo, "GBPUSD", hour, hour)


# Month boundary -------------------------------------------------------------


# Acceptance 6: determinism ------------------------------------------------


def test_byte_identical_parquet_on_repeat_resample(repo: Path, tmp_path: Path):
    from jarvis.bars.store import bars_path

    hour = _hour_ns(2024, 1, 15, 3)
    _write_blob(
        repo,
        "GBPUSD",
        hour,
        [
            (0, 100000, 99900, 1.0, 1.0),
            (500, 100010, 99910, 2.0, 2.0),
            (61_000, 100020, 99920, 1.5, 1.5),
        ],
    )

    resample_range(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))
    first_bytes = bars_path(repo, "GBPUSD", 2024, 1).read_bytes()

    resample_range(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))
    second_bytes = bars_path(repo, "GBPUSD", 2024, 1).read_bytes()

    assert first_bytes == second_bytes


def test_bars_span_month_boundary_written_to_two_files(repo: Path):
    hour_jan = _hour_ns(2024, 1, 31, 23)
    hour_feb = Nanos(hour_jan + NS_PER_HOUR)
    _write_blob(repo, "GBPUSD", hour_jan, [(0, 100000, 99900, 1.0, 1.0)])
    _write_blob(repo, "GBPUSD", hour_feb, [(0, 100010, 99910, 1.0, 1.0)])

    report = resample_range(repo, "GBPUSD", hour_jan, Nanos(hour_feb + NS_PER_HOUR))
    assert set(report.months_written) == {"2024-01", "2024-02"}
    assert report.bars_written == 2

    df = read_bars(repo, "GBPUSD", hour_jan, Nanos(hour_feb + NS_PER_HOUR)).sort("ts_utc_ns")
    assert df.height == 2
    assert df["ts_utc_ns"].to_list() == [hour_jan, hour_feb]
