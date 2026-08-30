from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from jarvis.core.errors import IntegrityError
from jarvis.core.types import Nanos
from jarvis.ingest.fetch_log import (
    FetchLogEntry,
    fetch_log_path,
    merge_fetch_log,
    read_fetch_log,
)


def _ns(y: int, m: int, d: int, h: int = 0) -> Nanos:
    return Nanos(int(datetime(y, m, d, h, tzinfo=timezone.utc).timestamp()) * 1_000_000_000)


def _entry(
    hour_ns: Nanos,
    status: str = "fetched",
    attempts: int = 1,
    byte_count: int = 100,
    error: str | None = None,
) -> FetchLogEntry:
    return FetchLogEntry(
        hour_utc_ns=hour_ns,
        status=status,
        attempts=attempts,
        byte_count=byte_count,
        recorded_utc="2026-08-30T11:00:00.000Z",
        error=error,
    )


def test_path_buckets_by_month(tmp_path: Path):
    hour = _ns(2024, 1, 15, 3)
    path = fetch_log_path(tmp_path, "GBPUSD", hour)
    assert path == (
        tmp_path / "data" / "raw" / "ticks" / "GBPUSD" / "_fetch_log" / "2024-01.json"
    )


def test_read_missing_log_returns_empty_dict(tmp_path: Path):
    assert read_fetch_log(tmp_path, "GBPUSD", 2024, 1) == {}


def test_read_malformed_log_raises_integrity_error(tmp_path: Path):
    path = fetch_log_path(tmp_path, "GBPUSD", _ns(2024, 1, 15))
    path.parent.mkdir(parents=True)
    path.write_text("not valid json {{{", encoding="utf-8")
    with pytest.raises(IntegrityError):
        read_fetch_log(tmp_path, "GBPUSD", 2024, 1)


def test_read_unknown_schema_version_raises_integrity_error(tmp_path: Path):
    path = fetch_log_path(tmp_path, "GBPUSD", _ns(2024, 1, 15))
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"instrument": "GBPUSD", "month": "2024-01", "schema_version": 99, "hours": {}}',
        encoding="utf-8",
    )
    with pytest.raises(IntegrityError):
        read_fetch_log(tmp_path, "GBPUSD", 2024, 1)


def test_merge_creates_file_and_roundtrips(tmp_path: Path):
    hour = _ns(2024, 1, 15, 3)
    entry = _entry(hour)
    merge_fetch_log(tmp_path, "GBPUSD", [entry])

    path = fetch_log_path(tmp_path, "GBPUSD", hour)
    assert path.is_file()

    result = read_fetch_log(tmp_path, "GBPUSD", 2024, 1)
    assert result == {hour: entry}


def test_merge_overwrites_same_hour(tmp_path: Path):
    hour = _ns(2024, 1, 15, 3)
    merge_fetch_log(tmp_path, "GBPUSD", [_entry(hour, status="missing", error="HTTP 500")])
    merge_fetch_log(tmp_path, "GBPUSD", [_entry(hour, status="fetched", error=None)])

    result = read_fetch_log(tmp_path, "GBPUSD", 2024, 1)
    assert result[hour].status == "fetched"
    assert result[hour].error is None


def test_merge_preserves_other_hours_in_month(tmp_path: Path):
    hour1 = _ns(2024, 1, 15, 3)
    hour2 = _ns(2024, 1, 16, 5)
    merge_fetch_log(tmp_path, "GBPUSD", [_entry(hour1)])
    merge_fetch_log(tmp_path, "GBPUSD", [_entry(hour2)])

    result = read_fetch_log(tmp_path, "GBPUSD", 2024, 1)
    assert set(result) == {hour1, hour2}


def test_skipped_existing_is_not_written(tmp_path: Path):
    hour = _ns(2024, 1, 15, 3)
    merge_fetch_log(tmp_path, "GBPUSD", [_entry(hour, status="fetched")])
    merge_fetch_log(
        tmp_path, "GBPUSD", [_entry(hour, status="skipped_existing", attempts=0)]
    )

    result = read_fetch_log(tmp_path, "GBPUSD", 2024, 1)
    assert result[hour].status == "fetched"  # unchanged, not overwritten


def test_entries_spanning_two_months_write_two_files(tmp_path: Path):
    hour_jan = _ns(2024, 1, 15, 3)
    hour_feb = _ns(2024, 2, 1, 0)
    merge_fetch_log(tmp_path, "GBPUSD", [_entry(hour_jan), _entry(hour_feb)])

    jan_path = fetch_log_path(tmp_path, "GBPUSD", hour_jan)
    feb_path = fetch_log_path(tmp_path, "GBPUSD", hour_feb)
    assert jan_path != feb_path
    assert jan_path.is_file()
    assert feb_path.is_file()

    jan_log = read_fetch_log(tmp_path, "GBPUSD", 2024, 1)
    feb_log = read_fetch_log(tmp_path, "GBPUSD", 2024, 2)
    assert set(jan_log) == {hour_jan}
    assert set(feb_log) == {hour_feb}


def test_write_is_atomic_no_partial_file_on_failure(tmp_path: Path):
    hour = _ns(2024, 1, 15, 3)
    merge_fetch_log(tmp_path, "GBPUSD", [_entry(hour, status="fetched")])
    path = fetch_log_path(tmp_path, "GBPUSD", hour)
    original_content = path.read_text(encoding="utf-8")

    with patch.object(Path, "write_text", side_effect=OSError("disk full")):
        with pytest.raises(IntegrityError):
            merge_fetch_log(tmp_path, "GBPUSD", [_entry(hour, status="missing")])

    assert path.read_text(encoding="utf-8") == original_content
    assert list(path.parent.glob("*.tmp")) == []
