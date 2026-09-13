from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest

from jarvis.core.errors import IntegrityError, UserError
from jarvis.core.hashing import sha256_file
from jarvis.core.types import Nanos
from jarvis.bars.resample import NS_PER_MINUTE, resample_range
from jarvis.bars.store import bars_path, read_bars
from jarvis.ingest.histdata_import import TICK_SCHEMA, tick_path, write_ticks
from jarvis.timeengine import NS_PER_HOUR

_POINT_SCALE = 1.0e-5


def _hour_ns(y: int, mo: int, d: int, h: int) -> Nanos:
    return Nanos(int(datetime(y, mo, d, h, tzinfo=timezone.utc).timestamp()) * 1_000_000_000)


def _write_ticks(
    repo_root: Path, instrument: str, year: int, month: int, records: list[tuple]
) -> None:
    """records: (ts_utc_ns, bid, ask, bid_volume, ask_volume) tuples, in the
    exact order they should be assigned row_sequence -- FILE ORDER, the
    same tie-break a real HistData import assigns (D-058). Writes via the
    real write_ticks (not a hand-built Parquet file) so these tests
    exercise the actual merge/dedup path a real import would go through."""
    frame = pl.DataFrame(
        {
            "ts_utc_ns": [r[0] for r in records],
            "bid": [r[1] for r in records],
            "ask": [r[2] for r in records],
            "bid_volume": [r[3] for r in records],
            "ask_volume": [r[4] for r in records],
            "row_sequence": list(range(len(records))),
        },
        schema=TICK_SCHEMA,
    )
    write_ticks(repo_root, instrument, year, month, frame)


@pytest.fixture
def repo(isolated_repo: Path) -> Path:
    (isolated_repo / "config" / "instruments.yaml").write_text(
        "GBPUSD:\n  point_scale: 1.0e-5\n  digits: 5\n", encoding="utf-8"
    )
    return isolated_repo


def _make_repo(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch, label: str) -> Path:
    """A second, independently-isolated repo root, for tests that need to
    compare two separate resample histories against each other. Leaves
    `jarvis.core.config.repo_root` pointed at the returned path; callers
    doing further work against a DIFFERENT repo afterward must repatch it."""
    root = tmp_path_factory.mktemp(label)
    (root / "pyproject.toml").write_text("", encoding="utf-8")
    (root / "config").mkdir()
    (root / "config" / "instruments.yaml").write_text(
        "GBPUSD:\n  point_scale: 1.0e-5\n  digits: 5\n", encoding="utf-8"
    )
    monkeypatch.setattr("jarvis.core.config.repo_root", lambda: root)
    return root


# Acceptance 2: absent-minute semantics -----------------------------------


def test_absent_minutes_produce_no_rows(repo: Path):
    hour = _hour_ns(2024, 1, 15, 3)
    # Ticks only in minute 0 and minute 5; minutes 1-4 must be entirely absent.
    _write_ticks(
        repo,
        "GBPUSD",
        2024,
        1,
        [
            (hour, 0.99900, 1.00000, 1.0, 1.0),
            (hour + 5 * NS_PER_MINUTE, 0.99910, 1.00010, 1.0, 1.0),
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


def test_present_month_with_no_ticks_in_range_produces_zero_bars(repo: Path):
    """A month file that exists on disk but contributes zero ticks to the
    REQUESTED sub-range is not a hole -- only a wholly-absent month file
    is (see test_hole_raises_by_default). Ticks exist elsewhere in the
    same month, just outside the hour being resampled."""
    hour = _hour_ns(2024, 1, 15, 3)
    elsewhere = _hour_ns(2024, 1, 20, 3)
    _write_ticks(repo, "GBPUSD", 2024, 1, [(elsewhere, 0.99900, 1.00000, 1.0, 1.0)])

    report = resample_range(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))
    assert report.bars_written == 0
    assert report.ticks_read == 0
    assert report.months_missing == 0


def test_present_month_with_no_ticks_in_range_still_written_with_explicit_schema(repo: Path):
    """A present month contributing zero bars to the requested range must
    still have its bars file written, with BAR_SCHEMA's dtypes, not
    skipped or written as a column-less frame. Otherwise a later
    read_bars caller selecting e.g. bid_c hits a column error on data
    that is perfectly valid."""
    from jarvis.bars.store import BAR_SCHEMA

    hour1 = _hour_ns(2024, 1, 15, 3)
    hour2 = Nanos(hour1 + NS_PER_HOUR)
    elsewhere = _hour_ns(2024, 1, 20, 3)
    _write_ticks(repo, "GBPUSD", 2024, 1, [(elsewhere, 0.99900, 1.00000, 1.0, 1.0)])

    report = resample_range(repo, "GBPUSD", hour1, Nanos(hour2 + NS_PER_HOUR))
    assert report.bars_written == 0
    assert report.months_written == ("2024-01",)
    assert bars_path(repo, "GBPUSD", 2024, 1).is_file()

    df = read_bars(repo, "GBPUSD", hour1, Nanos(hour2 + NS_PER_HOUR))
    assert df.height == 0
    assert df.schema == pl.Schema(BAR_SCHEMA)


# Acceptance 3: bid_h/ask_h from different ticks ---------------------------


def test_bid_high_and_ask_high_sourced_from_different_ticks(repo: Path):
    hour = _hour_ns(2024, 1, 15, 3)
    # Tick A: high bid, low ask. Tick B: low bid, high ask.
    # The naive-wrong implementation (derive both sides from a mid price)
    # would not be able to produce this combination at all.
    _write_ticks(
        repo,
        "GBPUSD",
        2024,
        1,
        [
            (hour, 0.99990, 1.00000, 1.0, 1.0),  # bid high
            (hour + 1_000_000, 0.99900, 1.00050, 1.0, 1.0),  # ask high
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
    _write_ticks(
        repo,
        "GBPUSD",
        2024,
        1,
        [
            (hour + 10 * 1_000_000_000, 1.00000, 1.00010, 1.0, 1.0),  # spread 0.00010
            (hour + 30 * 1_000_000_000, 1.00010, 1.00030, 1.0, 1.0),  # spread 0.00020
            (hour + 45 * 1_000_000_000, 1.00030, 1.00040, 1.0, 1.0),  # spread 0.00010
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
    _write_ticks(
        repo, "GBPUSD", 2024, 1, [(hour + 10 * 1_000_000_000, 1.00000, 1.00025, 1.0, 1.0)]
    )

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
    _write_ticks(repo, "GBPUSD", 2024, 1, [(hour, 0.99900, 1.00000, 1.0, 1.0)])

    resample_range(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))
    df = read_bars(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))
    assert df.row(0, named=True)["prev_gap_ns"] is None


def test_prev_gap_ns_correct_across_hour_boundary(repo: Path):
    hour1 = _hour_ns(2024, 1, 15, 3)
    hour2 = Nanos(hour1 + NS_PER_HOUR)

    last_tick_of_first = hour1 + 3_599_000 * 1_000_000  # 59:59 into hour1
    first_tick_of_second = hour2 + 500 * 1_000_000  # 500ms into hour2
    _write_ticks(
        repo,
        "GBPUSD",
        2024,
        1,
        [
            (last_tick_of_first, 0.99900, 1.00000, 1.0, 1.0),
            (first_tick_of_second, 0.99910, 1.00010, 1.0, 1.0),
        ],
    )

    resample_range(repo, "GBPUSD", hour1, Nanos(hour2 + NS_PER_HOUR))
    df = read_bars(repo, "GBPUSD", hour1, Nanos(hour2 + NS_PER_HOUR)).sort("ts_utc_ns")

    assert df.height == 2
    first_row, second_row = df.row(0, named=True), df.row(1, named=True)
    assert first_row["prev_gap_ns"] is None
    assert second_row["prev_gap_ns"] == first_tick_of_second - last_tick_of_first


def test_prev_gap_ns_carries_across_a_quiet_hour(repo: Path):
    """A hour with zero ticks in the middle of an otherwise-present month
    is simply a gap in the tick data -- not a distinct classification the
    resampler has to reason about (unlike the old Dukascopy per-hour
    empty/unfetched split). prev_gap_ns must still span the full quiet
    interval, unbroken by it."""
    hour1 = _hour_ns(2024, 1, 15, 3)
    hour3 = Nanos(hour1 + 2 * NS_PER_HOUR)  # hour2 is quiet: no ticks at all

    _write_ticks(
        repo,
        "GBPUSD",
        2024,
        1,
        [
            (hour1, 0.99900, 1.00000, 1.0, 1.0),
            (hour3, 0.99910, 1.00010, 1.0, 1.0),
        ],
    )

    resample_range(repo, "GBPUSD", hour1, Nanos(hour3 + NS_PER_HOUR))
    df = read_bars(repo, "GBPUSD", hour1, Nanos(hour3 + NS_PER_HOUR)).sort("ts_utc_ns")

    assert df.height == 2
    gap = df.row(1, named=True)["prev_gap_ns"]
    assert gap == hour3 - hour1  # gap spans the whole quiet hour, not reset by it


# Item 4: hole detection (WP-010: a HOLE is now a wholly-missing MONTH) -----


def test_hole_raises_by_default(repo: Path):
    hour_jan = _hour_ns(2024, 1, 31, 23)
    hour_feb = Nanos(hour_jan + NS_PER_HOUR)
    _write_ticks(repo, "GBPUSD", 2024, 1, [(hour_jan, 0.99900, 1.00000, 1.0, 1.0)])
    # February: no tick file at all -- a hole.

    with pytest.raises(IntegrityError):
        resample_range(repo, "GBPUSD", hour_jan, Nanos(hour_feb + NS_PER_HOUR))


def test_hole_proceeds_under_allow_incomplete(repo: Path):
    hour_jan = _hour_ns(2024, 1, 31, 23)
    hour_feb = Nanos(hour_jan + NS_PER_HOUR)
    _write_ticks(repo, "GBPUSD", 2024, 1, [(hour_jan, 0.99900, 1.00000, 1.0, 1.0)])

    report = resample_range(
        repo, "GBPUSD", hour_jan, Nanos(hour_feb + NS_PER_HOUR), allow_incomplete=True
    )
    assert report.months_missing == 1
    assert report.missing_months == ("2024-02",)
    assert report.bars_written == 1  # only January resampled
    assert "2024-02" not in report.months_written  # no bars file written for the hole


def test_allow_incomplete_does_not_default_to_true(repo: Path):
    import inspect

    sig = inspect.signature(resample_range)
    assert sig.parameters["allow_incomplete"].default is False


# Open/close tick order: WP-010 / D-058 row_sequence tie-break --------------


def test_open_and_close_use_row_sequence_order_not_numeric_sort(repo: Path):
    """Two ticks legitimately sharing an identical ts_utc_ns (D-058) must
    both be preserved -- not collapsed -- and the bar's open/close must
    reflect row_sequence (file) order, not a numeric sort of price."""
    hour = _hour_ns(2024, 1, 15, 3)
    # Same millisecond, tie-broken by row_sequence: the first-written
    # record is the "open" tick even though its ask is numerically larger.
    _write_ticks(
        repo,
        "GBPUSD",
        2024,
        1,
        [
            (hour, 0.99950, 1.00050, 1.0, 1.0),  # row_sequence 0 -> open
            (hour, 0.99910, 1.00010, 1.0, 1.0),  # row_sequence 1 -> close
        ],
    )

    report = resample_range(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))
    assert report.bars_written == 1

    df = read_bars(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))
    row = df.row(0, named=True)
    assert row["tick_count"] == 2  # both same-millisecond ticks preserved, not deduped
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


# Acceptance 6: determinism ------------------------------------------------


def test_byte_identical_parquet_on_repeat_resample(repo: Path, tmp_path: Path):
    hour = _hour_ns(2024, 1, 15, 3)
    _write_ticks(
        repo,
        "GBPUSD",
        2024,
        1,
        [
            (hour, 0.99900, 1.00000, 1.0, 1.0),
            (hour + 500_000_000, 0.99910, 1.00010, 2.0, 2.0),
            (hour + 61_000_000_000, 0.99920, 1.00020, 1.5, 1.5),
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
    _write_ticks(repo, "GBPUSD", 2024, 1, [(hour_jan, 0.99900, 1.00000, 1.0, 1.0)])
    _write_ticks(repo, "GBPUSD", 2024, 2, [(hour_feb, 0.99910, 1.00010, 1.0, 1.0)])

    report = resample_range(repo, "GBPUSD", hour_jan, Nanos(hour_feb + NS_PER_HOUR))
    assert set(report.months_written) == {"2024-01", "2024-02"}
    assert report.bars_written == 2

    df = read_bars(repo, "GBPUSD", hour_jan, Nanos(hour_feb + NS_PER_HOUR)).sort("ts_utc_ns")
    assert df.height == 2
    assert df["ts_utc_ns"].to_list() == [hour_jan, hour_feb]


# WP-005-CORRECTION: write_bars merge semantics ------------------------------


def test_resampling_second_day_preserves_first(repo: Path):
    """Both days live in the SAME month's tick file (as a real HistData
    import would produce) -- calling resample_range separately for each
    day's own hour range must not let the second call destroy the first
    day's already-written bars (WP-005-CORRECTION's write_bars merge)."""
    day1_hour = _hour_ns(2024, 1, 15, 3)
    day2_hour = _hour_ns(2024, 1, 16, 3)
    _write_ticks(
        repo,
        "GBPUSD",
        2024,
        1,
        [
            (day1_hour, 0.99900, 1.00000, 1.0, 1.0),
            (day2_hour, 0.99910, 1.00010, 1.0, 1.0),
        ],
    )

    resample_range(repo, "GBPUSD", day1_hour, Nanos(day1_hour + NS_PER_HOUR))
    resample_range(repo, "GBPUSD", day2_hour, Nanos(day2_hour + NS_PER_HOUR))

    df = read_bars(repo, "GBPUSD", day1_hour, Nanos(day2_hour + NS_PER_HOUR)).sort("ts_utc_ns")
    assert df.height == 2
    assert df["ts_utc_ns"].to_list() == [day1_hour, day2_hour]


def test_reresampling_same_range_is_idempotent(repo: Path):
    hour = _hour_ns(2024, 1, 15, 3)
    _write_ticks(
        repo,
        "GBPUSD",
        2024,
        1,
        [
            (hour, 0.99900, 1.00000, 1.0, 1.0),
            (hour + 60_000_000_000, 0.99910, 1.00010, 1.0, 1.0),
        ],
    )

    resample_range(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))
    path = bars_path(repo, "GBPUSD", 2024, 1)
    hash_a = sha256_file(path)

    resample_range(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))
    hash_b = sha256_file(path)

    assert hash_a == hash_b
    df = read_bars(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))
    assert df.height == 2  # no duplicate rows accumulated


def test_reresample_overwrites_changed_minute(repo: Path):
    """Simulates a corrected re-import: write_ticks' own re-import-must-win
    semantics (D-058) mean writing the same (ts_utc_ns, row_sequence)
    identity with new prices replaces the old tick, which must then flow
    through to a re-resample overwriting the previously-stored bar."""
    hour = _hour_ns(2024, 1, 15, 3)
    _write_ticks(repo, "GBPUSD", 2024, 1, [(hour, 0.99900, 1.00000, 1.0, 1.0)])
    resample_range(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))

    # Same (ts_utc_ns, row_sequence=0) identity, different prices -- as if
    # the month had been re-imported with a correction.
    _write_ticks(repo, "GBPUSD", 2024, 1, [(hour, 1.99900, 2.00000, 1.0, 1.0)])
    resample_range(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))

    df = read_bars(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))
    assert df.height == 1
    row = df.row(0, named=True)
    assert row["ask_o"] == pytest.approx(2.00000, abs=1e-9)
    assert row["bid_o"] == pytest.approx(1.99900, abs=1e-9)


def test_prev_gap_ns_correct_across_merge(
    repo: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
):
    day1_hour = _hour_ns(2024, 1, 15, 3)
    day2_hour = _hour_ns(2024, 1, 16, 3)
    last_tick_day1 = day1_hour + 3_599_000 * 1_000_000
    first_tick_day2 = day2_hour + 500 * 1_000_000
    _write_ticks(
        repo,
        "GBPUSD",
        2024,
        1,
        [
            (last_tick_day1, 0.99900, 1.00000, 1.0, 1.0),
            (first_tick_day2, 0.99910, 1.00010, 1.0, 1.0),
        ],
    )

    # Resample day 16 FIRST, then day 15 (out of order) -- each call only
    # covers its own single hour, reading from the SAME already-fully-
    # populated January tick file both times.
    resample_range(repo, "GBPUSD", day2_hour, Nanos(day2_hour + NS_PER_HOUR))
    resample_range(repo, "GBPUSD", day1_hour, Nanos(day1_hour + NS_PER_HOUR))
    merged_df = read_bars(
        repo, "GBPUSD", day1_hour, Nanos(day2_hour + NS_PER_HOUR)
    ).sort("ts_utc_ns")

    # Reference: the whole span resampled in a single pass, in a separate
    # repo. No allow_incomplete needed -- the month itself is fully
    # present (WP-010: a hole is a missing MONTH, not a quiet stretch
    # within one), even though most of it has no ticks.
    other_repo = _make_repo(tmp_path_factory, monkeypatch, "single_pass")
    _write_ticks(
        other_repo,
        "GBPUSD",
        2024,
        1,
        [
            (last_tick_day1, 0.99900, 1.00000, 1.0, 1.0),
            (first_tick_day2, 0.99910, 1.00010, 1.0, 1.0),
        ],
    )
    resample_range(other_repo, "GBPUSD", day1_hour, Nanos(day2_hour + NS_PER_HOUR))
    single_pass_df = read_bars(
        other_repo, "GBPUSD", day1_hour, Nanos(day2_hour + NS_PER_HOUR)
    ).sort("ts_utc_ns")

    assert merged_df.height == single_pass_df.height == 2
    assert merged_df["prev_gap_ns"].to_list() == single_pass_df["prev_gap_ns"].to_list()
    assert merged_df["prev_gap_ns"].to_list()[0] is None
    assert merged_df["prev_gap_ns"].to_list()[1] is not None


def test_merged_output_matches_single_pass(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
):
    """The property that actually matters: how the same logical range was
    split across resample_range calls must not affect the stored result."""
    day1_hour = _hour_ns(2024, 1, 15, 3)
    day2_hour = _hour_ns(2024, 1, 16, 3)
    records = [
        (day1_hour, 0.99900, 1.00000, 1.0, 1.0),
        (day2_hour, 0.99910, 1.00010, 1.0, 1.0),
    ]

    one_call_repo = _make_repo(tmp_path_factory, monkeypatch, "one_call")
    _write_ticks(one_call_repo, "GBPUSD", 2024, 1, records)
    resample_range(one_call_repo, "GBPUSD", day1_hour, Nanos(day2_hour + NS_PER_HOUR))
    hash_a = sha256_file(bars_path(one_call_repo, "GBPUSD", 2024, 1))

    two_call_repo = _make_repo(tmp_path_factory, monkeypatch, "two_calls")
    _write_ticks(two_call_repo, "GBPUSD", 2024, 1, records)
    resample_range(two_call_repo, "GBPUSD", day1_hour, Nanos(day1_hour + NS_PER_HOUR))
    resample_range(two_call_repo, "GBPUSD", day2_hour, Nanos(day2_hour + NS_PER_HOUR))
    hash_b = sha256_file(bars_path(two_call_repo, "GBPUSD", 2024, 1))

    assert hash_a == hash_b


# WP-010: end-to-end regression against a real divergent-era month ----------


def test_resample_real_divergent_month_end_to_end(repo: Path):
    """Regression test required by WP-010: resample a real HistData-style
    month end-to-end from data/tick/, confirming sane, non-empty output.
    Uses a synthetic but realistic tick set shaped like 2020-03 (a
    timezone-switch month, D-055) rather than the actual multi-hundred-MB
    archive file, which is not available to the test suite -- the point
    here is exercising the real read path (tick_path -> pl.read_parquet
    -> filter -> resample), not re-deriving D-055's own already-verified
    timezone correctness."""
    year, month = 2020, 3
    base = _hour_ns(year, month, 9, 12)  # a Monday, safely mid-month
    records = [
        (base + i * 10_000_000_000, 1.29000 + i * 0.00001, 1.29010 + i * 0.00001, 1.0, 1.0)
        for i in range(50)
    ]
    _write_ticks(repo, "GBPUSD", year, month, records)

    report = resample_range(repo, "GBPUSD", base, Nanos(base + NS_PER_HOUR))
    assert report.months_with_data == 1
    assert report.months_missing == 0
    assert report.ticks_read == 50
    assert report.bars_written > 0

    df = read_bars(repo, "GBPUSD", base, Nanos(base + NS_PER_HOUR))
    assert df.height == report.bars_written
    assert df["tick_count"].sum() == 50
    assert (df["ask_c"] >= df["bid_c"]).all()
