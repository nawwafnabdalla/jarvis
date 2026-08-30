import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from jarvis.core.errors import IntegrityError, UserError
from jarvis.core.types import Nanos
from jarvis.ingest.fetch import RawBlob, _check_results_complete, fetch_hour, ingest_range
from jarvis.ingest.urls import raw_blob_path


def _ns(y: int, m: int, d: int, h: int = 0) -> Nanos:
    return Nanos(int(datetime(y, m, d, h, tzinfo=timezone.utc).timestamp()) * 1_000_000_000)


class _FakeResponse:
    def __init__(self, status_code: int, content: bytes = b"", headers: dict | None = None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}


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
            tmp_path / "a", "GBPUSD", start, end, concurrency=1, min_seconds_between_requests=0
        )
        report4 = ingest_range(
            tmp_path / "b", "GBPUSD", start, end, concurrency=4, min_seconds_between_requests=0
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
        report = ingest_range(tmp_path, "GBPUSD", start, end, min_seconds_between_requests=0)
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
            ingest_range(tmp_path, "GBPUSD", start, end, concurrency=4, min_seconds_between_requests=0)


# --- Item 2 (A-7): global rate limit -------------------------------------


def test_global_rate_limit_is_not_per_worker(tmp_path: Path):
    """At the old per-worker sleep, concurrency=4 would give an aggregate
    rate 4x the intended one -- elapsed time for 8 hours would be roughly
    2 x min_seconds_between_requests, not 7x. A global rate limit must
    space every request, across all workers combined, by at least
    min_seconds_between_requests."""
    start = _ns(2024, 1, 15, 0)
    end = _ns(2024, 1, 15, 8)
    with patch("jarvis.ingest.fetch.requests.get", return_value=_FakeResponse(200, b"x")):
        t0 = time.perf_counter()
        report = ingest_range(
            tmp_path,
            "GBPUSD",
            start,
            end,
            concurrency=4,
            min_seconds_between_requests=0.1,
        )
        elapsed = time.perf_counter() - t0
    assert report.hours_fetched == 8
    assert elapsed >= 7 * 0.1


def test_skipped_hours_do_not_consume_rate_budget(tmp_path: Path):
    start = _ns(2024, 1, 15, 0)
    end = _ns(2024, 1, 15, 8)
    with patch("jarvis.ingest.fetch.requests.get", return_value=_FakeResponse(200, b"x")):
        ingest_range(
            tmp_path, "GBPUSD", start, end, concurrency=4, min_seconds_between_requests=0.1
        )

    with patch("jarvis.ingest.fetch.requests.get") as mock_get:
        t0 = time.perf_counter()
        report = ingest_range(
            tmp_path, "GBPUSD", start, end, concurrency=4, min_seconds_between_requests=0.1
        )
        elapsed = time.perf_counter() - t0

    mock_get.assert_not_called()
    assert report.hours_skipped_existing == 8
    assert elapsed < 0.5


# --- Item 3 (A-8): 429 handling -------------------------------------------


def test_429_uses_retry_after_header_when_present(tmp_path: Path):
    hour = _ns(2024, 1, 15, 3)
    sleeps: list[float] = []
    responses = [_FakeResponse(429, headers={"Retry-After": "5"}), _FakeResponse(200, b"data")]
    with (
        patch("jarvis.ingest.fetch.requests.get", side_effect=responses),
        patch("jarvis.ingest.fetch.time.sleep", side_effect=sleeps.append),
    ):
        blob = fetch_hour(tmp_path, "GBPUSD", hour, max_attempts=2)
    assert blob.status == "fetched"
    assert sleeps == [5.0]


def test_429_falls_back_to_aggressive_backoff_without_header(tmp_path: Path):
    hour = _ns(2024, 1, 15, 3)
    sleeps: list[float] = []
    with (
        patch("jarvis.ingest.fetch.requests.get", return_value=_FakeResponse(429)),
        patch("jarvis.ingest.fetch.time.sleep", side_effect=sleeps.append),
    ):
        fetch_hour(tmp_path, "GBPUSD", hour, max_attempts=4)
    assert sleeps == [60.0, 120.0, 240.0]


def test_429_http_date_retry_after_falls_back_to_default_backoff(tmp_path: Path):
    hour = _ns(2024, 1, 15, 3)
    sleeps: list[float] = []
    with (
        patch(
            "jarvis.ingest.fetch.requests.get",
            return_value=_FakeResponse(
                429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
            ),
        ),
        patch("jarvis.ingest.fetch.time.sleep", side_effect=sleeps.append),
    ):
        fetch_hour(tmp_path, "GBPUSD", hour, max_attempts=2)
    # An HTTP-date Retry-After falls back to the default aggressive
    # backoff (60 * 2^0), not date arithmetic.
    assert sleeps == [60.0]


def test_exhausted_429_classified_rate_limited_not_missing(tmp_path: Path):
    hour = _ns(2024, 1, 15, 3)
    with (
        patch("jarvis.ingest.fetch.requests.get", return_value=_FakeResponse(429)),
        patch("jarvis.ingest.fetch.time.sleep"),
    ):
        blob = fetch_hour(tmp_path, "GBPUSD", hour, max_attempts=3)
    assert blob.status == "rate_limited"
    assert blob.path is None
    assert blob.error == "HTTP 429"


def test_sustained_rate_limiting_raises_integrity_error(tmp_path: Path):
    start = _ns(2024, 1, 15, 0)
    end = _ns(2024, 1, 15, 5)
    with patch("jarvis.ingest.fetch.requests.get", return_value=_FakeResponse(429)):
        with pytest.raises(IntegrityError):
            ingest_range(
                tmp_path,
                "GBPUSD",
                start,
                end,
                concurrency=1,
                max_attempts=1,
                max_consecutive_rate_limits=3,
                min_seconds_between_requests=0,
            )


def test_max_attempts_and_timeout_are_plumbed_through_ingest_range(tmp_path: Path):
    start = _ns(2024, 1, 15, 0)
    end = _ns(2024, 1, 15, 1)
    with (
        patch("jarvis.ingest.fetch.fetch_hour", wraps=fetch_hour) as mock_fetch_hour,
        patch("jarvis.ingest.fetch.requests.get", return_value=_FakeResponse(200, b"x")),
    ):
        ingest_range(
            tmp_path,
            "GBPUSD",
            start,
            end,
            max_attempts=7,
            timeout_seconds=12.5,
            min_seconds_between_requests=0,
        )
    _, kwargs = mock_fetch_hour.call_args
    assert kwargs["max_attempts"] == 7
    assert kwargs["timeout_seconds"] == 12.5


# --- Item 4 (A-11): completeness check, not a filter ----------------------


def test_unfilled_result_slot_raises_integrity_error():
    hours = [_ns(2024, 1, 15, 0), _ns(2024, 1, 15, 1), _ns(2024, 1, 15, 2)]

    results = [
        RawBlob(
            instrument="GBPUSD",
            hour_utc_ns=hours[0],
            status="fetched",
            byte_count=1,
            path=None,
            attempts=1,
            error=None,
        ),
        None,
        RawBlob(
            instrument="GBPUSD",
            hour_utc_ns=hours[2],
            status="fetched",
            byte_count=1,
            path=None,
            attempts=1,
            error=None,
        ),
    ]
    with pytest.raises(IntegrityError):
        _check_results_complete(hours, results)
