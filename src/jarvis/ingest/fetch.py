"""Raw Dukascopy tick blob fetching: retry, resumability, concurrency,
a global rate limit, and 429-aware backoff."""

import concurrent.futures
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

import requests

from jarvis.core.errors import IntegrityError, UserError
from jarvis.core.types import Nanos
from jarvis.ingest.urls import NS_PER_HOUR, dukascopy_url, raw_blob_path

RawStatus = Literal["fetched", "empty", "missing", "skipped_existing", "rate_limited"]


@dataclass(frozen=True, slots=True)
class RawBlob:
    instrument: str
    hour_utc_ns: Nanos
    status: RawStatus
    byte_count: int
    path: Path | None  # None only when status is "missing" or "rate_limited"
    attempts: int
    error: str | None  # populated only when status is "missing" or "rate_limited"


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
    hours_rate_limited: int
    missing_hours: tuple[Nanos, ...]
    rate_limited_hours: tuple[Nanos, ...]
    started_utc: str
    completed_utc: str
    total_bytes: int


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


_RetryOutcome = Literal["success", "empty", "rate_limited", "retryable_error"]


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header. Returns seconds if the value is an
    integer; None if absent, or if it parses as an HTTP-date instead of an
    integer -- callers fall back to the default backoff rather than
    attempting date arithmetic, per spec."""
    if value is None:
        return None
    try:
        return float(int(value.strip()))
    except ValueError:
        return None


def _fetch_once(
    url: str, timeout_seconds: float
) -> tuple[_RetryOutcome, bytes, str | None, float | None]:
    try:
        response = requests.get(url, timeout=timeout_seconds)
    except requests.exceptions.RequestException as exc:
        return "retryable_error", b"", str(exc), None

    if response.status_code == 200:
        content = response.content
        return ("success" if content else "empty"), content, None, None
    if response.status_code == 404:
        return "empty", b"", None, None
    if response.status_code == 429:
        retry_after = _parse_retry_after(response.headers.get("Retry-After"))
        return "rate_limited", b"", "HTTP 429", retry_after
    return "retryable_error", b"", f"HTTP {response.status_code}", None


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
    max_backoff_seconds: float = 900.0,
    rate_limit_wait: Callable[[], None] | None = None,
) -> RawBlob:
    """Fetch one hour. See "Required behaviour" for the full state table.

    rate_limit_wait, if given, is called immediately before every network
    request this call makes (the first attempt and every retry) -- the
    global rate limit applies to each request, not once per hour."""
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
    last_outcome: _RetryOutcome = "retryable_error"
    for attempt in range(1, max_attempts + 1):
        if rate_limit_wait is not None:
            rate_limit_wait()

        outcome, content, error, retry_after = _fetch_once(url, timeout_seconds)
        last_outcome = outcome

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
            if outcome == "rate_limited":
                delay = (
                    retry_after
                    if retry_after is not None
                    else min(60.0 * 2 ** (attempt - 1), max_backoff_seconds)
                )
            else:
                delay = 2 ** (attempt - 1)
            time.sleep(delay)

    final_status: RawStatus = "rate_limited" if last_outcome == "rate_limited" else "missing"
    return RawBlob(
        instrument=instrument,
        hour_utc_ns=hour_utc_ns,
        status=final_status,
        byte_count=0,
        path=None,
        attempts=max_attempts,
        error=last_error,
    )


class _GlobalRateLimiter:
    """A mutex-guarded "next allowed request time" shared across every
    worker thread -- the fix for A-7: the previous per-worker sleep let
    each of `concurrency` workers run its own independent clock, so the
    aggregate rate was concurrency times the intended one.

    The lock is held only long enough to atomically reserve a strictly
    increasing time slot; each thread then sleeps for its own slot outside
    the lock, so threads don't block each other's sleeping."""

    def __init__(self, min_seconds_between_requests: float) -> None:
        self._min_seconds = min_seconds_between_requests
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            scheduled = max(self._next_allowed, now)
            self._next_allowed = scheduled + self._min_seconds
        remaining = scheduled - now
        if remaining > 0:
            time.sleep(remaining)


def _check_results_complete(hours: list[Nanos], results: list["RawBlob | None"]) -> list[RawBlob]:
    """Raise IntegrityError if any slot is unfilled, rather than silently
    dropping it via a filter (A-11) -- an unreachable state (a worker
    exiting without ever assigning its slot) must present as a crash, not
    as a plausible-looking short report. Post-WP-001-CORRECTION every
    worker exception propagates via future.result(), so this should be
    unreachable in practice; it is an explicit completeness check, not a
    filter, for exactly that reason."""
    unfilled = [hours[i] for i, b in enumerate(results) if b is None]
    if unfilled:
        raise IntegrityError(
            f"{len(unfilled)} of {len(hours)} hours have no result "
            f"(first unaccounted: {unfilled[0]}); this indicates a worker "
            "exited without completing its assigned hours"
        )
    return results  # type: ignore[return-value]


def ingest_range(
    repo_root: Path,
    instrument: str,
    start_ns: Nanos,
    end_ns: Nanos,
    *,
    force_refetch: bool = False,
    concurrency: int = 4,
    min_seconds_between_requests: float = 0.25,
    max_attempts: int = 3,
    timeout_seconds: float = 30.0,
    max_backoff_seconds: float = 900.0,
    max_consecutive_rate_limits: int = 10,
) -> IngestReport:
    """Fetch every UTC hour in [start_ns, end_ns). start_ns and end_ns must
    both be hour-aligned; raise UserError otherwise. Uses a bounded worker
    pool of `concurrency` workers sharing one global rate limit: no two
    network requests across all workers combined start less than
    min_seconds_between_requests apart. Skipped (already on disk) hours
    make no network request and so do not consume rate-limit budget.

    Raises IntegrityError if max_consecutive_rate_limits hours in a row
    (counting only hours that actually hit the network, not skipped ones)
    come back rate_limited -- a sustained 429 wall should stop the run and
    tell the operator, not grind through the remaining range producing
    worthless records."""
    if start_ns % NS_PER_HOUR != 0 or end_ns % NS_PER_HOUR != 0:
        raise UserError(
            f"start_ns ({start_ns}) and end_ns ({end_ns}) must both be hour-aligned"
        )
    if start_ns >= end_ns:
        raise UserError(f"start_ns ({start_ns}) must be strictly before end_ns ({end_ns})")

    hours: list[Nanos] = [Nanos(ns) for ns in range(start_ns, end_ns, NS_PER_HOUR)]
    results: list[RawBlob | None] = [None] * len(hours)

    started_utc = _utc_now_iso()

    # Deferred import: fetch_log.py imports RawStatus from this module at
    # its own top level, so a module-level import here would be circular.
    # Both modules are fully loaded by the time ingest_range is actually
    # called, so a function-scoped import is safe and standard for
    # breaking a sibling-module cycle like this one.
    from jarvis.ingest.fetch_log import FetchLogEntry, merge_fetch_log

    rate_limiter = _GlobalRateLimiter(min_seconds_between_requests)
    streak_lock = threading.Lock()
    consecutive_rate_limited = [0]
    fetch_log_lock = threading.Lock()

    def worker(worker_index: int) -> None:
        for i in range(worker_index, len(hours), concurrency):
            hour_ns = hours[i]
            path = raw_blob_path(repo_root, instrument, hour_ns)
            will_hit_network = force_refetch or not path.is_file()

            blob = fetch_hour(
                repo_root,
                instrument,
                hour_ns,
                force_refetch=force_refetch,
                max_attempts=max_attempts,
                timeout_seconds=timeout_seconds,
                max_backoff_seconds=max_backoff_seconds,
                rate_limit_wait=rate_limiter.wait if will_hit_network else None,
            )
            results[i] = blob

            if not will_hit_network:
                continue  # skipped_existing: does not touch the streak counter, not logged

            # Written immediately, not batched to the end of the range, so
            # a halt from the streak check below (or any other worker's
            # exception) does not discard provenance for hours already
            # fetched in this run. Lock-protected because merge_fetch_log
            # does a read-modify-write on one JSON file per month, and two
            # workers can hit the same month concurrently.
            with fetch_log_lock:
                merge_fetch_log(
                    repo_root,
                    instrument,
                    [
                        FetchLogEntry(
                            hour_utc_ns=blob.hour_utc_ns,
                            status=blob.status,
                            attempts=blob.attempts,
                            byte_count=blob.byte_count,
                            recorded_utc=_utc_now_iso(),
                            error=blob.error,
                        )
                    ],
                )

            with streak_lock:
                if blob.status == "rate_limited":
                    consecutive_rate_limited[0] += 1
                    if consecutive_rate_limited[0] >= max_consecutive_rate_limits:
                        raise IntegrityError(
                            f"{consecutive_rate_limited[0]} consecutive hours rate-limited; "
                            "halting rather than continuing to grind through a sustained "
                            "429 wall -- retry later with a longer min_seconds_between_requests"
                        )
                else:
                    consecutive_rate_limited[0] = 0

    if concurrency <= 1:
        worker(0)
    else:
        # ThreadPoolExecutor + future.result() in submission order, per
        # WP-001-CORRECTION: a raw Thread swallows a raised exception
        # silently. As there, there is no requirement to cancel in-flight
        # workers once one raises -- the halt condition here is the same
        # "raise rather than return a plausible-looking partial success"
        # pattern, not a hard real-time deadline.
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(worker, w) for w in range(concurrency)]
            for future in futures:
                future.result()

    completed_utc = _utc_now_iso()

    blobs = _check_results_complete(hours, results)

    return IngestReport(
        instrument=instrument,
        range_start_ns=start_ns,
        range_end_ns=end_ns,
        hours_expected=len(hours),
        hours_fetched=sum(1 for b in blobs if b.status == "fetched"),
        hours_empty=sum(1 for b in blobs if b.status == "empty"),
        hours_missing=sum(1 for b in blobs if b.status == "missing"),
        hours_skipped_existing=sum(1 for b in blobs if b.status == "skipped_existing"),
        hours_rate_limited=sum(1 for b in blobs if b.status == "rate_limited"),
        missing_hours=tuple(b.hour_utc_ns for b in blobs if b.status == "missing"),
        rate_limited_hours=tuple(b.hour_utc_ns for b in blobs if b.status == "rate_limited"),
        started_utc=started_utc,
        completed_utc=completed_utc,
        total_bytes=sum(b.byte_count for b in blobs),
    )
