import hashlib
import shutil
import zipfile
from pathlib import Path

import polars as pl
import pytest

from jarvis.core.hashing import sha256_file
from jarvis.ingest.histdata_import import (
    import_histdata,
    import_log_path,
    read_import_log,
    tick_path,
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


# Merge semantics (D-045) ----------------------------------------------


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


def test_output_schema_matches_tick_columns(tmp_path: Path):
    from jarvis.ingest.parse import TickArrays

    repo_root = tmp_path / "repo"
    source = tmp_path / "source"
    _copy_month_to_dir(source, _MAY_CSV, _MAY_TXT)
    import_histdata(repo_root, source, "GBPUSD", start=(2017, 5), end=(2017, 5))

    df = pl.read_parquet(tick_path(repo_root, "GBPUSD", 2017, 5))
    tickarrays_columns = {"ts_utc_ns", "bid", "ask", "bid_volume", "ask_volume"}
    assert set(df.columns) == tickarrays_columns
    assert df.schema["ts_utc_ns"] == pl.Int64
    assert df.schema["bid"] == pl.Float64
    assert df.schema["ask"] == pl.Float64
    assert df.schema["bid_volume"] == pl.Float64
    assert df.schema["ask_volume"] == pl.Float64
