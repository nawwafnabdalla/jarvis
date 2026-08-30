from pathlib import Path
from unittest.mock import patch

import polars as pl
import pytest

from jarvis.bars.store import BAR_SCHEMA, bars_path, read_bars, write_bars
from jarvis.core.errors import IntegrityError


def _sample_frame(ts_values: list[int]) -> pl.DataFrame:
    n = len(ts_values)
    return pl.DataFrame(
        {
            "ts_utc_ns": ts_values,
            "bid_o": [1.0] * n,
            "bid_h": [1.0] * n,
            "bid_l": [1.0] * n,
            "bid_c": [1.0] * n,
            "ask_o": [1.0001] * n,
            "ask_h": [1.0001] * n,
            "ask_l": [1.0001] * n,
            "ask_c": [1.0001] * n,
            "tick_count": [1] * n,
            "first_tick_ns": ts_values,
            "last_tick_ns": ts_values,
            "spread_open": [0.0001] * n,
            "spread_max": [0.0001] * n,
            "spread_twa": [0.0001] * n,
            "prev_gap_ns": [None] * n,
        },
        schema=BAR_SCHEMA,
    )


def test_bars_path_layout(tmp_path: Path):
    path = bars_path(tmp_path, "GBPUSD", 2024, 1)
    assert path == (
        tmp_path / "data" / "bars_1m" / "instrument=GBPUSD" / "year=2024" / "2024-01.parquet"
    )


_JAN_1_2024_NS = 1_704_067_200_000_000_000  # 2024-01-01T00:00:00Z


def test_write_and_read_roundtrip(tmp_path: Path):
    ts = [_JAN_1_2024_NS, _JAN_1_2024_NS + 60_000_000_000, _JAN_1_2024_NS + 120_000_000_000]
    frame = _sample_frame(ts)
    path = write_bars(tmp_path, "GBPUSD", 2024, 1, frame)
    assert path.is_file()

    result = read_bars(tmp_path, "GBPUSD", _JAN_1_2024_NS, _JAN_1_2024_NS + 200_000_000_000)
    assert result["ts_utc_ns"].to_list() == ts


def test_read_bars_filters_to_requested_range(tmp_path: Path):
    ts = [_JAN_1_2024_NS, _JAN_1_2024_NS + 60_000_000_000, _JAN_1_2024_NS + 120_000_000_000]
    write_bars(tmp_path, "GBPUSD", 2024, 1, _sample_frame(ts))

    result = read_bars(tmp_path, "GBPUSD", ts[0] + 1, ts[2] + 1)
    assert result["ts_utc_ns"].to_list() == [ts[1], ts[2]]


def test_read_bars_missing_month_contributes_no_rows(tmp_path: Path):
    # No file written at all for this range.
    result = read_bars(tmp_path, "GBPUSD", _JAN_1_2024_NS, _JAN_1_2024_NS + 1_000_000_000_000)
    assert result.height == 0
    assert set(result.columns) == set(BAR_SCHEMA)


def test_read_bars_spans_two_month_files_ascending_order(tmp_path: Path):
    jan_ns = 1_704_067_200_000_000_000  # 2024-01-01T00:00:00Z
    feb_ns = 1_706_745_600_000_000_000  # 2024-02-01T00:00:00Z
    write_bars(tmp_path, "GBPUSD", 2024, 2, _sample_frame([feb_ns]))
    write_bars(tmp_path, "GBPUSD", 2024, 1, _sample_frame([jan_ns]))

    result = read_bars(tmp_path, "GBPUSD", jan_ns, feb_ns + 1)
    assert result["ts_utc_ns"].to_list() == [jan_ns, feb_ns]  # ascending, not insertion order


def test_write_is_atomic_no_partial_file_on_failure(tmp_path: Path):
    frame = _sample_frame([1_000])
    write_bars(tmp_path, "GBPUSD", 2024, 1, frame)
    path = bars_path(tmp_path, "GBPUSD", 2024, 1)
    original_bytes = path.read_bytes()

    with patch("os.replace", side_effect=OSError("disk full")):
        with pytest.raises(IntegrityError):
            write_bars(tmp_path, "GBPUSD", 2024, 1, _sample_frame([2_000]))

    assert path.read_bytes() == original_bytes
    assert list(path.parent.glob("*.tmp")) == []


def test_byte_identical_output_on_repeat_write(tmp_path: Path):
    """Determinism within one environment (same polars/pyarrow versions),
    per the documented limitation in write_bars -- NOT claimed across
    environments, since Parquet embeds a writer version string."""
    frame = _sample_frame([1_000, 2_000, 3_000])
    path_a = write_bars(tmp_path, "GBPUSD", 2024, 1, frame)
    bytes_a = path_a.read_bytes()

    path_b = write_bars(tmp_path, "GBPUSD", 2024, 1, frame)
    bytes_b = path_b.read_bytes()

    assert bytes_a == bytes_b
