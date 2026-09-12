import hashlib
import shutil
import zipfile
from pathlib import Path

import polars as pl
import pytest

from jarvis.core.errors import IntegrityError
from jarvis.core.hashing import sha256_file
from jarvis.ingest.histdata_import import (
    TICK_SCHEMA,
    import_histdata,
    import_log_path,
    read_import_log,
    tick_path,
    write_ticks,
)

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "histdata"
_MAY_CSV = _FIXTURE_DIR / "DAT_ASCII_GBPUSD_T_201705.csv"
_MAY_TXT = _FIXTURE_DIR / "DAT_ASCII_GBPUSD_T_201705.txt"
_JUN_CSV = _FIXTURE_DIR / "DAT_ASCII_GBPUSD_T_201706.csv"
_JUN_TXT = _FIXTURE_DIR / "DAT_ASCII_GBPUSD_T_201706.txt"


def _copy_month_to_dir(dest_dir: Path, csv_src: Path, txt_src: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(csv_src, dest_dir / csv_src.name)
    shutil.copy2(txt_src, dest_dir / txt_src.name)


def _zip_month(dest_zip: Path, csv_src: Path, txt_src: Path) -> None:
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest_zip, "w") as zf:
        zf.write(csv_src, csv_src.name)
        zf.write(txt_src, txt_src.name)


def _sha256_of_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# Merge semantics -------------------------------------------------------
#
# write_ticks and write_bars (jarvis.bars.store) share a merge-then-dedup
# -then-sort SHAPE -- write_ticks was written by copying write_bars'
# established pattern -- but they are separate functions in separate
# modules, storing structurally different things, and D-045 (which fixed
# write_bars' replace-instead-of-merge defect) never reviewed either
# function's DEDUP KEY choice. The tests below exercise write_ticks
# directly; see write_ticks's own docstring for the full lineage note.


def _tick_frame(rows: list[tuple[int, float, float]]) -> pl.DataFrame:
    """Build a minimal, schema-correct tick frame from (ts_utc_ns, bid,
    ask) tuples. row_sequence is 0-indexed by construction order, exactly
    as _histdata_month_to_frame produces it from real source-file order."""
    return pl.DataFrame(
        {
            "ts_utc_ns": [r[0] for r in rows],
            "bid": [r[1] for r in rows],
            "ask": [r[2] for r in rows],
            "bid_volume": [None] * len(rows),
            "ask_volume": [None] * len(rows),
            "row_sequence": range(len(rows)),
        },
        schema=TICK_SCHEMA,
    )


def test_write_ticks_preserves_distinct_same_millisecond_quotes(tmp_path: Path):
    """WP-009g / D-058: two rows sharing ts_utc_ns but carrying DIFFERENT
    quotes are the exact case the old ts_utc_ns-only dedup key silently
    discarded (WP-009f measured up to 38% of a month lost this way). Both
    must now survive, distinguished by row_sequence."""
    repo_root = tmp_path / "repo"
    frame = _tick_frame(
        [
            (1_000_000_000_000, 1.30000, 1.30010),  # same ts_utc_ns,
            (1_000_000_000_000, 1.29990, 1.30000),  # genuinely different quote
        ]
    )
    write_ticks(repo_root, "GBPUSD", 2010, 4, frame)

    on_disk = pl.read_parquet(tick_path(repo_root, "GBPUSD", 2010, 4))
    assert on_disk.height == 2
    assert set(on_disk["ts_utc_ns"].to_list()) == {1_000_000_000_000}
    assert sorted(on_disk["bid"].to_list()) == [1.29990, 1.30000]
    assert sorted(zip(on_disk["ts_utc_ns"], on_disk["row_sequence"])) == [
        (1_000_000_000_000, 0),
        (1_000_000_000_000, 1),
    ]


def test_write_ticks_still_dedupes_true_full_duplicates(tmp_path: Path):
    """A TRUE full duplicate -- identical ts_utc_ns AND bid AND ask, a
    literally repeated row rather than a distinct quote -- must still
    collapse to one row, exactly as before this fix. It carries no
    information the compound key needs to preserve."""
    repo_root = tmp_path / "repo"
    frame = _tick_frame(
        [
            (1_000_000_000_000, 1.30000, 1.30010),
            (1_000_000_000_000, 1.30000, 1.30010),  # byte-identical repeat
        ]
    )
    write_ticks(repo_root, "GBPUSD", 2010, 4, frame)

    on_disk = pl.read_parquet(tick_path(repo_root, "GBPUSD", 2010, 4))
    assert on_disk.height == 1
    assert on_disk["ts_utc_ns"][0] == 1_000_000_000_000
    assert on_disk["bid"][0] == pytest.approx(1.30000)
    assert on_disk["ask"][0] == pytest.approx(1.30010)


def test_write_ticks_mixed_collisions_and_duplicates_reduced_scale_2010_04(tmp_path: Path):
    """Regression fixture at reduced scale for the real 2010-04 shape
    (WP-009f: 38.0% of that month's rows were genuine collisions). One
    millisecond carries three genuinely different quotes (all must
    survive); a second millisecond carries a real quote plus one true
    full duplicate of it (must collapse to two rows, not three); a third
    millisecond is an ordinary single tick (unaffected)."""
    repo_root = tmp_path / "repo"
    frame = _tick_frame(
        [
            (2_000_000_000_000, 1.51110, 1.51140),  # group A: 3 distinct quotes
            (2_000_000_000_000, 1.51120, 1.51150),
            (2_000_000_000_000, 1.51100, 1.51130),
            (2_000_060_000_000, 1.53180, 1.53240),  # group B: quote + its own duplicate
            (2_000_060_000_000, 1.53170, 1.53230),
            (2_000_060_000_000, 1.53170, 1.53230),  # true duplicate of the row above
            (2_000_120_000_000, 1.52000, 1.52010),  # group C: ordinary, no collision
        ]
    )
    write_ticks(repo_root, "GBPUSD", 2010, 4, frame)

    on_disk = pl.read_parquet(tick_path(repo_root, "GBPUSD", 2010, 4)).sort(
        ["ts_utc_ns", "row_sequence"]
    )
    # 3 (all distinct) + 2 (one true duplicate collapsed) + 1 (ordinary) = 6
    assert on_disk.height == 6
    group_a = on_disk.filter(pl.col("ts_utc_ns") == 2_000_000_000_000)
    assert group_a.height == 3
    assert sorted(group_a["bid"].to_list()) == [1.51100, 1.51110, 1.51120]
    group_b = on_disk.filter(pl.col("ts_utc_ns") == 2_000_060_000_000)
    assert group_b.height == 2
    assert sorted(group_b["bid"].to_list()) == [1.53170, 1.53180]
    group_c = on_disk.filter(pl.col("ts_utc_ns") == 2_000_120_000_000)
    assert group_c.height == 1


def test_write_ticks_reimport_still_wins_under_compound_key(tmp_path: Path):
    """The original "a re-import must win" guarantee must survive the key
    change: re-writing the same (ts_utc_ns, row_sequence) identity with a
    corrected price must replace the stale row, not add a second one."""
    repo_root = tmp_path / "repo"
    write_ticks(repo_root, "GBPUSD", 2010, 4, _tick_frame([(1_000_000_000_000, 1.30000, 1.30010)]))
    write_ticks(
        repo_root, "GBPUSD", 2010, 4, _tick_frame([(1_000_000_000_000, 1.31111, 1.31121)])
    )  # same ts_utc_ns AND row_sequence=0 -- a correction to the same tick, not a new one

    on_disk = pl.read_parquet(tick_path(repo_root, "GBPUSD", 2010, 4))
    assert on_disk.height == 1
    assert on_disk["bid"][0] == pytest.approx(1.31111)


def test_write_ticks_refuses_to_merge_against_pre_row_sequence_schema(tmp_path: Path):
    """A tick file written before WP-009g has no row_sequence column.
    Merging fresh data onto it must fail loudly, naming the reason --
    never silently coerce nulls into the new key or crash with an opaque
    polars schema-mismatch error. The file must be rebuilt from source."""
    repo_root = tmp_path / "repo"
    path = tick_path(repo_root, "GBPUSD", 2010, 4)
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy = pl.DataFrame(
        {"ts_utc_ns": [1_000_000_000_000], "bid": [1.3], "ask": [1.31],
         "bid_volume": [None], "ask_volume": [None]},
        schema={"ts_utc_ns": pl.Int64, "bid": pl.Float64, "ask": pl.Float64,
                "bid_volume": pl.Float64, "ask_volume": pl.Float64},
    )
    legacy.write_parquet(path)

    with pytest.raises(IntegrityError, match="row_sequence"):
        write_ticks(repo_root, "GBPUSD", 2010, 4, _tick_frame([(2_000_000_000_000, 1.3, 1.31)]))


def test_two_months_import_both_present(tmp_path: Path):
    repo_root = tmp_path / "repo"
    source = tmp_path / "source"
    _copy_month_to_dir(source, _MAY_CSV, _MAY_TXT)
    _copy_month_to_dir(source, _JUN_CSV, _JUN_TXT)

    report_a = import_histdata(repo_root, source, "GBPUSD", start=(2017, 5), end=(2017, 5))
    assert report_a.months_imported == 1
    report_b = import_histdata(repo_root, source, "GBPUSD", start=(2017, 6), end=(2017, 6))
    assert report_b.months_imported == 1

    may_path = tick_path(repo_root, "GBPUSD", 2017, 5)
    jun_path = tick_path(repo_root, "GBPUSD", 2017, 6)
    assert may_path.is_file()
    assert jun_path.is_file()
    assert pl.read_parquet(may_path).height == 5
    assert pl.read_parquet(jun_path).height == 3


def test_reimport_is_idempotent_byte_identical(tmp_path: Path):
    repo_root = tmp_path / "repo"
    source = tmp_path / "source"
    _copy_month_to_dir(source, _MAY_CSV, _MAY_TXT)

    import_histdata(repo_root, source, "GBPUSD", start=(2017, 5), end=(2017, 5))
    path = tick_path(repo_root, "GBPUSD", 2017, 5)
    hash_a = sha256_file(path)

    import_histdata(repo_root, source, "GBPUSD", start=(2017, 5), end=(2017, 5), force=True)
    hash_b = sha256_file(path)

    assert hash_a == hash_b
    assert pl.read_parquet(path).height == 5  # no duplicate rows accumulated


def test_reimport_without_force_is_skipped(tmp_path: Path):
    repo_root = tmp_path / "repo"
    source = tmp_path / "source"
    _copy_month_to_dir(source, _MAY_CSV, _MAY_TXT)

    report_a = import_histdata(repo_root, source, "GBPUSD", start=(2017, 5), end=(2017, 5))
    assert report_a.months_imported == 1

    report_b = import_histdata(repo_root, source, "GBPUSD", start=(2017, 5), end=(2017, 5))
    assert report_b.months_imported == 0
    assert "2017-05" in report_b.months_skipped


# Provenance -------------------------------------------------------------


def test_import_log_records_stamp_clock_evidence_sha256(tmp_path: Path):
    """May contains no date on which the US and EU DST calendars
    disagree, so both stamp clocks convert it identically and no
    determination is made. The log must say so explicitly rather than
    leaving a bare null for a reader to interpret."""
    repo_root = tmp_path / "repo"
    source = tmp_path / "source"
    _copy_month_to_dir(source, _MAY_CSV, _MAY_TXT)

    import_histdata(repo_root, source, "GBPUSD", start=(2017, 5), end=(2017, 5))

    log = read_import_log(repo_root, "GBPUSD", 2017, 5)
    assert log is not None
    assert log["stamp_clock"] is None
    assert log["stamp_clock_determination"] == "not_required_both_clocks_agree"
    assert "no date on which the US and EU DST calendars disagree" in log["stamp_clock_evidence"]
    assert log["column_order"] == "bid_ask"
    assert log["column_order_determination"] == "detected_whole_file"
    assert log["source_sha256"] == _sha256_of_file(source / "DAT_ASCII_GBPUSD_T_201705.csv")
    assert log["row_count"] == 5
    assert log["source_filename"] == "DAT_ASCII_GBPUSD_T_201705.csv"


def test_import_log_records_a_detected_stamp_clock(tmp_path: Path):
    """A March file's clock DOES matter, so the log must record which one
    was detected and the evidence it rested on."""
    repo_root = tmp_path / "repo"
    source = tmp_path / "source"
    source.mkdir(parents=True, exist_ok=True)
    (source / "DAT_ASCII_GBPUSD_T_202203.csv").write_text(
        "\n".join(
            [
                "20220318 155900000,1.31000,1.31010,0",  # divergent Friday close
                "20220320 160100000,1.32000,1.32010,0",  # Sunday reopen
                "20220327 185942410,1.33000,1.33010,0",  # last tick before the switch
                "20220327 200000088,1.33100,1.33110,0",  # first tick after it
                "20220328 120000000,1.34000,1.34010,0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (source / "DAT_ASCII_GBPUSD_T_202203.txt").write_text("", encoding="utf-8")

    import_histdata(repo_root, source, "GBPUSD", start=(2022, 3), end=(2022, 3))

    log = read_import_log(repo_root, "GBPUSD", 2022, 3)
    assert log["stamp_clock"] == "eu_dst"
    assert log["stamp_clock_determination"] == "detected_from_file_content"
    assert "2022-03-18" in log["stamp_clock_evidence"]
    assert "spring clock switch verified" in log["stamp_clock_evidence"]
    assert log["column_order"] == "bid_ask"
    assert log["column_order_determination"] == "detected_whole_file"


def test_import_log_records_mixed_per_day_column_order(tmp_path: Path):
    """A file that switches column order mid-month at a weekend boundary
    (2009-05's shape, D-055h / D-056) must be imported, not refused, and
    the log must say explicitly that the per-day fallback was needed --
    not just report a value that happens to differ."""
    repo_root = tmp_path / "repo"
    source = tmp_path / "source"
    source.mkdir(parents=True, exist_ok=True)
    (source / "DAT_ASCII_GBPUSD_T_201705.csv").write_text(
        "\n".join(
            [
                "20170515 120000000,1.29020,1.29010,0",  # ask_bid segment
                "20170517 120000000,1.29030,1.29020,0",
                "20170519 165900000,1.29040,1.29030,0",  # Friday close
                "20170521 170100000,1.29000,1.29010,0",  # Sunday reopen -- switch
                "20170522 120000000,1.29010,1.29020,0",  # bid_ask segment
                "20170524 120000000,1.29020,1.29030,0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (source / "DAT_ASCII_GBPUSD_T_201705.txt").write_text("", encoding="utf-8")

    import_histdata(repo_root, source, "GBPUSD", start=(2017, 5), end=(2017, 5))

    log = read_import_log(repo_root, "GBPUSD", 2017, 5)
    assert log["column_order"] == "mixed_per_day"
    assert log["column_order_determination"] == "detected_per_day_at_weekend_boundary"
    assert "2017-05-19" in log["column_order_evidence"]
    assert "2017-05-21" in log["column_order_evidence"]
    assert log["row_count"] == 6


def test_gap_count_parsed_from_txt(tmp_path: Path):
    repo_root = tmp_path / "repo"
    source = tmp_path / "source"
    _copy_month_to_dir(source, _MAY_CSV, _MAY_TXT)

    report = import_histdata(repo_root, source, "GBPUSD", start=(2017, 5), end=(2017, 5))
    assert report.gap_reports["2017-05"] == 2  # 2 "Gap of ...s found" lines in the fixture .txt

    log = read_import_log(repo_root, "GBPUSD", 2017, 5)
    assert log["declared_gaps"] == 2


# Source discovery ---------------------------------------------------------


def test_directory_of_csvs(tmp_path: Path):
    repo_root = tmp_path / "repo"
    source = tmp_path / "source"
    _copy_month_to_dir(source, _MAY_CSV, _MAY_TXT)
    _copy_month_to_dir(source, _JUN_CSV, _JUN_TXT)

    report = import_histdata(repo_root, source, "GBPUSD")
    assert report.months_found == 2
    assert report.months_imported == 2
    assert set(report.stamp_clocks) == {"2017-05", "2017-06"}


def test_directory_of_monthly_zips(tmp_path: Path):
    repo_root = tmp_path / "repo"
    source = tmp_path / "source"
    source.mkdir(parents=True)
    _zip_month(source / "HISTDATA_COM_ASCII_GBPUSD_T201705.zip", _MAY_CSV, _MAY_TXT)
    _zip_month(source / "HISTDATA_COM_ASCII_GBPUSD_T201706.zip", _JUN_CSV, _JUN_TXT)

    report = import_histdata(repo_root, source, "GBPUSD")
    assert report.months_found == 2
    assert report.months_imported == 2
    assert pl.read_parquet(tick_path(repo_root, "GBPUSD", 2017, 5)).height == 5
    assert pl.read_parquet(tick_path(repo_root, "GBPUSD", 2017, 6)).height == 3


def test_zip_of_monthly_zips(tmp_path: Path):
    repo_root = tmp_path / "repo"
    staging = tmp_path / "staging"
    staging.mkdir(parents=True)
    _zip_month(staging / "HISTDATA_COM_ASCII_GBPUSD_T201705.zip", _MAY_CSV, _MAY_TXT)
    _zip_month(staging / "HISTDATA_COM_ASCII_GBPUSD_T201706.zip", _JUN_CSV, _JUN_TXT)

    outer_zip = tmp_path / "ALL_DATA_GBPUSD.zip"
    with zipfile.ZipFile(outer_zip, "w") as zf:
        for inner in staging.iterdir():
            zf.write(inner, inner.name)

    report = import_histdata(repo_root, outer_zip, "GBPUSD")
    assert report.months_found == 2
    assert report.months_imported == 2
    assert pl.read_parquet(tick_path(repo_root, "GBPUSD", 2017, 5)).height == 5
    assert pl.read_parquet(tick_path(repo_root, "GBPUSD", 2017, 6)).height == 3


def test_start_end_range_filters_months(tmp_path: Path):
    repo_root = tmp_path / "repo"
    source = tmp_path / "source"
    _copy_month_to_dir(source, _MAY_CSV, _MAY_TXT)
    _copy_month_to_dir(source, _JUN_CSV, _JUN_TXT)

    report = import_histdata(repo_root, source, "GBPUSD", start=(2017, 6), end=(2017, 6))
    assert report.months_imported == 1
    assert "2017-05" in report.months_skipped
    assert not tick_path(repo_root, "GBPUSD", 2017, 5).is_file()
    assert tick_path(repo_root, "GBPUSD", 2017, 6).is_file()


# Output schema (acceptance criterion 4) ------------------------------------


def test_output_schema_matches_tick_columns_plus_row_sequence(tmp_path: Path):
    """The on-disk schema is TickArrays' columns PLUS row_sequence
    (WP-009g / D-058) -- deliberately not byte-identical to TickArrays
    any more. TickArrays never persists to Parquet (it's an ephemeral
    per-hour array; resample_range reads .bi5 blobs directly), so it
    never needed a stored tie-breaker column the way write_ticks's
    merged, persisted store does."""
    repo_root = tmp_path / "repo"
    source = tmp_path / "source"
    _copy_month_to_dir(source, _MAY_CSV, _MAY_TXT)
    import_histdata(repo_root, source, "GBPUSD", start=(2017, 5), end=(2017, 5))

    df = pl.read_parquet(tick_path(repo_root, "GBPUSD", 2017, 5))
    expected_columns = {"ts_utc_ns", "bid", "ask", "bid_volume", "ask_volume", "row_sequence"}
    assert set(df.columns) == expected_columns
    assert df.schema["ts_utc_ns"] == pl.Int64
    assert df.schema["bid"] == pl.Float64
    assert df.schema["ask"] == pl.Float64
    assert df.schema["bid_volume"] == pl.Float64
    assert df.schema["ask_volume"] == pl.Float64
    assert df.schema["row_sequence"] == pl.Int64
