"""Raw Dukascopy tick blob fetching: retry, resumability, concurrency."""

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import requests

from jarvis.core.errors import IntegrityError, UserError
from jarvis.core.types import Nanos
from jarvis.ingest.urls import NS_PER_HOUR, dukascopy_url, raw_blob_path

RawStatus = Literal["fetched", "empty", "missing", "skipped_existing"]


@dataclass(frozen=True, slots=True)
class RawBlob:
    instrument: str
    hour_utc_ns: Nanos
    status: RawStatus
    byte_count: int
    path: Path | None  # None only when status == "missing"
    attempts: int
    error: str | None  # populated only when status == "missing"


@dataclass(frozen=True, slots=True)
class IngestReport:
    instrument: str
    range_start_ns: Nanos
    range_end_ns: Nanos  # exclusive, half-open [start, end)
    hours_expected: int
    hours_fetched: int
    hours_empty: int
    hours_missing: int
    hours_skipped_existing: int
    missing_hours: tuple[Nanos, ...]
    started_utc: str
    completed_utc: str
    total_bytes: int


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


_RetryOutcome = Literal["success", "empty", "retryable_error"]


def _fetch_once(url: str, timeout_seconds: float) -> tuple[_RetryOutcome, bytes, str | None]:
    try:
        response = requests.get(url, timeout=timeout_seconds)
    except requests.exceptions.RequestException as exc:
        return "retryable_error", b"", str(exc)

    if response.status_code == 200:
        content = response.content
        return ("success" if content else "empty"), content, None
    if response.status_code == 404:
        return "empty", b"", None
    return "retryable_error", b"", f"HTTP {response.status_code}"


def _write_blob(path: Path, content: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    except OSError as exc:
        raise IntegrityError(f"failed to write raw blob to {path}: {exc}") from exc


def fetch_hour(
    repo_root: Path,
    instrument: str,
    hour_utc_ns: Nanos,
    *,
    force_refetch: bool = False,
    max_attempts: int = 3,
    timeout_seconds: float = 30.0,
) -> RawBlob:
    """Fetch one hour. See "Required behaviour" for the full state table."""
    path = raw_blob_path(repo_root, instrument, hour_utc_ns)

    if path.is_file() and not force_refetch:
        return RawBlob(
            instrument=instrument,
            hour_utc_ns=hour_utc_ns,
            status="skipped_existing",
            byte_count=path.stat().st_size,
            path=path,
            attempts=0,
            error=None,
        )

    url = dukascopy_url(instrument, hour_utc_ns)

    last_error: str | None = None
    for attempt in range(1, max_attempts + 1):
        outcome, content, error = _fetch_once(url, timeout_seconds)

        if outcome == "success":
            _write_blob(path, content)
            return RawBlob(
                instrument=instrument,
                hour_utc_ns=hour_utc_ns,
                status="fetched",
                byte_count=len(content),
                path=path,
                attempts=attempt,
                error=None,
            )

        if outcome == "empty":
            _write_blob(path, b"")
            return RawBlob(
                instrument=instrument,
                hour_utc_ns=hour_utc_ns,
                status="empty",
                byte_count=0,
                path=path,
                attempts=attempt,
                error=None,
            )

        last_error = error
        if attempt < max_attempts:
            time.sleep(2 ** (attempt - 1))

    return RawBlob(
        instrument=instrument,
        hour_utc_ns=hour_utc_ns,
        status="missing",
        byte_count=0,
        path=None,
        attempts=max_attempts,
        error=last_error,
    )


def ingest_range(
    repo_root: Path,
    instrument: str,
    start_ns: Nanos,
    end_ns: Nanos,
    *,
    force_refetch: bool = False,
    concurrency: int = 4,
    courtesy_delay_seconds: float = 0.2,
) -> IngestReport:
    """Fetch every UTC hour in [start_ns, end_ns). start_ns and end_ns must
    both be hour-aligned; raise UserError otherwise. Uses a bounded worker
    pool of `concurrency` workers, each pausing at least
    courtesy_delay_seconds between its own requests — this is a public data
    source with no documented rate limit, and 0B's job is to be a considerate
    client, not to find the ceiling."""
    if start_ns % NS_PER_HOUR != 0 or end_ns % NS_PER_HOUR != 0:
        raise UserError(
            f"start_ns ({start_ns}) and end_ns ({end_ns}) must both be hour-aligned"
        )
    if start_ns >= end_ns:
        raise UserError(f"start_ns ({start_ns}) must be strictly before end_ns ({end_ns})")

    hours: list[Nanos] = [Nanos(ns) for ns in range(start_ns, end_ns, NS_PER_HOUR)]
    results: list[RawBlob | None] = [None] * len(hours)

    started_utc = _utc_now_iso()

    def worker(worker_index: int) -> None:
        last_request_time: float | None = None
        for i in range(worker_index, len(hours), concurrency):
            hour_ns = hours[i]
            path = raw_blob_path(repo_root, instrument, hour_ns)
            will_hit_network = force_refetch or not path.is_file()

            if will_hit_network and last_request_time is not None:
                remaining = courtesy_delay_seconds - (time.monotonic() - last_request_time)
                if remaining > 0:
                    time.sleep(remaining)

            results[i] = fetch_hour(
                repo_root, instrument, hour_ns, force_refetch=force_refetch
            )

            if will_hit_network:
                last_request_time = time.monotonic()

    if concurrency <= 1:
        worker(0)
    else:
        threads = [threading.Thread(target=worker, args=(w,)) for w in range(concurrency)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    completed_utc = _utc_now_iso()

    blobs = [b for b in results if b is not None]
    return IngestReport(
        instrument=instrument,
        range_start_ns=start_ns,
        range_end_ns=end_ns,
        hours_expected=len(hours),
        hours_fetched=sum(1 for b in blobs if b.status == "fetched"),
        hours_empty=sum(1 for b in blobs if b.status == "empty"),
        hours_missing=sum(1 for b in blobs if b.status == "missing"),
        hours_skipped_existing=sum(1 for b in blobs if b.status == "skipped_existing"),
        missing_hours=tuple(b.hour_utc_ns for b in blobs if b.status == "missing"),
        started_utc=started_utc,
        completed_utc=completed_utc,
        total_bytes=sum(b.byte_count for b in blobs),
    )
