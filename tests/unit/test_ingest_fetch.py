from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from jarvis.core.errors import IntegrityError, UserError
from jarvis.core.types import Nanos
from jarvis.ingest.fetch import fetch_hour, ingest_range
from jarvis.ingest.urls import raw_blob_path


def _ns(y: int, m: int, d: int, h: int = 0) -> Nanos:
    return Nanos(int(datetime(y, m, d, h, tzinfo=timezone.utc).timestamp()) * 1_000_000_000)


class _FakeResponse:
    def __init__(self, status_code: int, content: bytes = b""):
        self.status_code = status_code
        self.content = content


def test_fetch_hour_200_nonempty_classified_fetched(tmp_path: Path):
    hour = _ns(2024, 1, 15, 3)
    with patch(
        "jarvis.ingest.fetch.requests.get", return_value=_FakeResponse(200, b"data")
    ) as mock_get:
        blob = fetch_hour(tmp_path, "GBPUSD", hour)
    assert blob.status == "fetched"
    assert blob.byte_count == 4
    assert blob.path is not None and blob.path.read_bytes() == b"data"
    assert mock_get.call_count == 1


def test_fetch_hour_200_empty_body_classified_empty(tmp_path: Path):
    hour = _ns(2024, 1, 15, 3)
    with patch("jarvis.ingest.fetch.requests.get", return_value=_FakeResponse(200, b"")):
        blob = fetch_hour(tmp_path, "GBPUSD", hour)
    assert blob.status == "empty"
    assert blob.byte_count == 0
    assert blob.path is not None and blob.path.is_file()


def test_fetch_hour_404_classified_empty(tmp_path: Path):
    hour = _ns(2024, 1, 15, 3)
    with patch("jarvis.ingest.fetch.requests.get", return_value=_FakeResponse(404)):
        blob = fetch_hour(tmp_path, "GBPUSD", hour)
    assert blob.status == "empty"
    assert blob.path is not None and blob.path.is_file()


def test_fetch_hour_exhausted_retries_classified_missing(tmp_path: Path):
    hour = _ns(2024, 1, 15, 3)
    with (
        patch("jarvis.ingest.fetch.requests.get", return_value=_FakeResponse(500)),
        patch("jarvis.ingest.fetch.time.sleep"),
    ):
        blob = fetch_hour(tmp_path, "GBPUSD", hour, max_attempts=3)
    assert blob.status == "missing"
    assert blob.attempts == 3
    assert blob.path is None
    assert blob.error is not None
    assert not raw_blob_path(tmp_path, "GBPUSD", hour).exists()


def test_fetch_hour_backoff_timing(tmp_path: Path):
    hour = _ns(2024, 1, 15, 3)
    sleeps: list[float] = []
    with (
        patch("jarvis.ingest.fetch.requests.get", return_value=_FakeResponse(500)),
        patch("jarvis.ingest.fetch.time.sleep", side_effect=sleeps.append),
    ):
        fetch_hour(tmp_path, "GBPUSD", hour, max_attempts=4)
    assert sleeps == [1, 2, 4]


def test_resumability_skips_existing_file_without_network_call(tmp_path: Path):
    hour = _ns(2024, 1, 15, 3)
    path = raw_blob_path(tmp_path, "GBPUSD", hour)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"pre-existing")
    with patch("jarvis.ingest.fetch.requests.get") as mock_get:
        blob = fetch_hour(tmp_path, "GBPUSD", hour)
    mock_get.assert_not_called()
    assert blob.status == "skipped_existing"
    assert blob.attempts == 0


def test_resumability_skips_zero_byte_existing_file(tmp_path: Path):
    hour = _ns(2024, 1, 15, 3)
    path = raw_blob_path(tmp_path, "GBPUSD", hour)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")
    with patch("jarvis.ingest.fetch.requests.get") as mock_get:
        blob = fetch_hour(tmp_path, "GBPUSD", hour)
    mock_get.assert_not_called()
    assert blob.status == "skipped_existing"


def test_force_refetch_ignores_existing_file(tmp_path: Path):
    hour = _ns(2024, 1, 15, 3)
    path = raw_blob_path(tmp_path, "GBPUSD", hour)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"stale")
    with patch(
        "jarvis.ingest.fetch.requests.get", return_value=_FakeResponse(200, b"fresh")
    ) as mock_get:
        blob = fetch_hour(tmp_path, "GBPUSD", hour, force_refetch=True)
    mock_get.assert_called_once()
    assert blob.status == "fetched"
    assert path.read_bytes() == b"fresh"


def test_ingest_range_rejects_non_hour_aligned_start(tmp_path: Path):
    start = Nanos(_ns(2024, 1, 15, 3) + 1)
    end = _ns(2024, 1, 15, 5)
    with patch("jarvis.ingest.fetch.requests.get") as mock_get:
        with pytest.raises(UserError):
            ingest_range(tmp_path, "GBPUSD", start, end)
    mock_get.assert_not_called()


def test_ingest_range_rejects_start_after_end(tmp_path: Path):
    start = _ns(2024, 1, 15, 5)
    end = _ns(2024, 1, 15, 3)
    with pytest.raises(UserError):
        ingest_range(tmp_path, "GBPUSD", start, end)


def test_ingest_range_concurrency_1_and_4_agree_on_counts(tmp_path: Path):
    start = _ns(2024, 1, 15, 0)
    end = _ns(2024, 1, 15, 10)

    with patch("jarvis.ingest.fetch.requests.get", return_value=_FakeResponse(200, b"x")):
        report1 = ingest_range(
            tmp_path / "a", "GBPUSD", start, end, concurrency=1, courtesy_delay_seconds=0
        )
        report4 = ingest_range(
            tmp_path / "b", "GBPUSD", start, end, concurrency=4, courtesy_delay_seconds=0
        )

    assert report1.hours_expected == report4.hours_expected == 10
    assert report1.hours_fetched == report4.hours_fetched == 10
    assert report1.hours_empty == report4.hours_empty == 0
    assert report1.hours_missing == report4.hours_missing == 0
    assert report1.total_bytes == report4.total_bytes


def test_disk_write_failure_raises_integrity_error_not_missing(tmp_path: Path):
    hour = _ns(2024, 1, 15, 3)
    with (
        patch("jarvis.ingest.fetch.requests.get", return_value=_FakeResponse(200, b"data")),
        patch.object(Path, "write_bytes", side_effect=OSError("disk full")),
    ):
        with pytest.raises(IntegrityError):
            fetch_hour(tmp_path, "GBPUSD", hour)


def test_weekend_range_all_empty_not_missing(tmp_path: Path):
    start = _ns(2024, 1, 13, 0)  # Saturday
    end = _ns(2024, 1, 13, 3)
    with patch("jarvis.ingest.fetch.requests.get", return_value=_FakeResponse(200, b"")):
        report = ingest_range(tmp_path, "GBPUSD", start, end, courtesy_delay_seconds=0)
    assert report.hours_missing == 0
    assert report.hours_empty == 3


def test_disk_write_failure_in_concurrent_worker_still_raises(tmp_path: Path):
    """The exact scenario that previously vanished silently: at concurrency > 1,
    a write failure must propagate out of ingest_range, not disappear along
    with the thread that hit it."""
    start = _ns(2024, 1, 15, 0)
    end = _ns(2024, 1, 15, 8)
    with (
        patch("jarvis.ingest.fetch.requests.get", return_value=_FakeResponse(200, b"data")),
        patch.object(Path, "write_bytes", side_effect=OSError("disk full")),
    ):
        with pytest.raises(IntegrityError):
            ingest_range(tmp_path, "GBPUSD", start, end, concurrency=4, courtesy_delay_seconds=0)
