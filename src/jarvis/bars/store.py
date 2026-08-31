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

BAR_SCHEMA: dict[str, pl.DataType] = {
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


def _recompute_prev_gap_ns(frame: pl.DataFrame) -> pl.DataFrame:
    """prev_gap_ns as a pure function of the (sorted) stored frame:
    first_tick_ns[i] - last_tick_ns[i-1], null for row 0. Recomputed here
    rather than trusting whatever a single resample run threaded through
    -- WP-005-CORRECTION: a resample run only knows the bar that preceded
    it WITHIN THAT RUN, which is wrong (or a spurious null) the moment two
    separate runs' output is merged into one month file, regardless of
    which order they were resampled in. `.shift(1)` gives a null in row 0
    automatically, which then propagates through the subtraction -- no
    special-casing needed."""
    return frame.with_columns(
        (pl.col("first_tick_ns") - pl.col("last_tick_ns").shift(1)).alias("prev_gap_ns")
    )


def write_bars(
    repo_root: Path, instrument: str, year: int, month: int, frame: pl.DataFrame
) -> Path:
    """Write one month of 1-minute bars, atomically (temp file in the same
    directory, then os.replace() -- same pattern and reasoning as the
    fetch log).

    MERGES with any existing month file rather than replacing it (WP-005-
    CORRECTION: replacing silently destroyed every previously-resampled
    hour in the same month whenever a later call only covered part of
    it). Existing rows and `frame`'s rows are concatenated, deduplicated
    on ts_utc_ns keeping `frame`'s row for any collision (a re-resample of
    the same minute must win over what was stored before), sorted
    ascending by ts_utc_ns, and prev_gap_ns is recomputed for the entire
    merged result -- see _recompute_prev_gap_ns.

    Deterministic: writing the same logical content twice produces
    byte-identical files WITHIN one environment (same polars/pyarrow
    versions) -- this holds whether that content arrived in one call or
    was assembled by merging several. This is NOT guaranteed across
    library versions -- Parquet embeds a `created_by` string carrying the
    writer version. The dataset manifest (Stage 1B) records library
    versions for exactly this reason.
    """
    path = bars_path(repo_root, instrument, year, month)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.is_file():
        existing = pl.read_parquet(path)
        combined = pl.concat([existing, frame])
    else:
        combined = frame

    merged = combined.unique(subset=["ts_utc_ns"], keep="last", maintain_order=True).sort(
        "ts_utc_ns"
    )
    merged = _recompute_prev_gap_ns(merged)

    tmp_path = path.with_name(path.name + ".tmp")
    try:
        merged.write_parquet(tmp_path, **_WRITE_PARQUET_KWARGS)
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
