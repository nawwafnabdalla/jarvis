"""Monthly Parquet storage for 1-minute bars (WP-005 item 3).

Path layout: data/bars_1m/instrument={instrument}/year={YYYY}/{YYYY}-{MM}.parquet
-- monthly files inside year-partitioned directories, matching the fetch
log's granularity (jarvis.ingest.fetch_log) so a single month can be
re-resampled independently without a read-modify-write of a whole year.
"""

import os
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from jarvis.core.errors import IntegrityError
from jarvis.core.types import Nanos

BAR_SCHEMA: dict[str, pl.DataType | type] = {
    "ts_utc_ns": pl.Int64,
    "bid_o": pl.Float64,
    "bid_h": pl.Float64,
    "bid_l": pl.Float64,
    "bid_c": pl.Float64,
    "ask_o": pl.Float64,
    "ask_h": pl.Float64,
    "ask_l": pl.Float64,
    "ask_c": pl.Float64,
    "tick_count": pl.Int32,
    "first_tick_ns": pl.Int64,
    "last_tick_ns": pl.Int64,
    "spread_open": pl.Float64,
    "spread_max": pl.Float64,
    "spread_twa": pl.Float64,
    "prev_gap_ns": pl.Int64,
}

_WRITE_PARQUET_KWARGS = {
    "compression": "zstd",
    "compression_level": 3,
    "statistics": True,
    "row_group_size": 1_000_000,
}


def bars_path(repo_root: Path, instrument: str, year: int, month: int) -> Path:
    return (
        repo_root
        / "data"
        / "bars_1m"
        / f"instrument={instrument}"
        / f"year={year:04d}"
        / f"{year:04d}-{month:02d}.parquet"
    )


def write_bars(
    repo_root: Path, instrument: str, year: int, month: int, frame: pl.DataFrame
) -> Path:
    """Write one month of 1-minute bars, atomically (temp file in the same
    directory, then os.replace() -- same pattern and reasoning as the
    fetch log).

    Deterministic: resampling the same input twice produces byte-identical
    files WITHIN one environment (same polars/pyarrow versions). This is
    NOT guaranteed across library versions -- Parquet embeds a
    `created_by` string carrying the writer version. The dataset manifest
    (Stage 1B) records library versions for exactly this reason.
    """
    path = bars_path(repo_root, instrument, year, month)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        frame.write_parquet(tmp_path, **_WRITE_PARQUET_KWARGS)
        os.replace(tmp_path, path)
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        raise IntegrityError(f"failed to write bars parquet {path}: {exc}") from exc
    return path


def _empty_bars_frame() -> pl.DataFrame:
    return pl.DataFrame(schema=BAR_SCHEMA)


def _months_between(start_ns: Nanos, end_ns: Nanos) -> list[tuple[int, int]]:
    if end_ns <= start_ns:
        return []
    start_dt = datetime.fromtimestamp(start_ns // 1_000_000_000, tz=timezone.utc)
    end_dt = datetime.fromtimestamp((end_ns - 1) // 1_000_000_000, tz=timezone.utc)

    months: list[tuple[int, int]] = []
    year, month = start_dt.year, start_dt.month
    while (year, month) <= (end_dt.year, end_dt.month):
        months.append((year, month))
        month += 1
        if month == 13:
            month = 1
            year += 1
    return months


def read_bars(repo_root: Path, instrument: str, start_ns: Nanos, end_ns: Nanos) -> pl.DataFrame:
    """Read bars in [start_ns, end_ns), across every month file overlapping
    the range, returned in ascending ts_utc_ns order. A month with no
    Parquet file on disk simply contributes no rows -- not an error."""
    frames = []
    for year, month in _months_between(start_ns, end_ns):
        path = bars_path(repo_root, instrument, year, month)
        if not path.is_file():
            continue
        frames.append(pl.read_parquet(path))

    if not frames:
        return _empty_bars_frame()

    combined = pl.concat(frames)
    filtered = combined.filter(
        (pl.col("ts_utc_ns") >= start_ns) & (pl.col("ts_utc_ns") < end_ns)
    )
    return filtered.sort("ts_utc_ns")
