"""Fetch-level provenance sidecar (A-3 / D-038).

Records what the network told us for each hour, so the resampler and QA
can distinguish an hour where the market was closed (0-byte blob, status
"empty") from an hour that was never successfully fetched (no blob,
status "missing" or "rate_limited"). Both currently produce zero bars
downstream with nothing else recording the difference.

This is fetch-level provenance, subordinate to and later consumable by the
dataset manifest (Stage 1B) -- not a duplicate of it. It does not hash or
seal anything.

Deliberately does not import jarvis.timeengine: Dukascopy hours are
UTC-aligned by construction (the same reasoning that kept urls.py, WP-001,
on stdlib datetime), and the month bucket here is plain UTC calendar
arithmetic. The existing ingest-restriction import-linter contract does
not forbid timeengine for this module -- this is a deliberate design
choice for consistency with urls.py, not an enforced one.
"""

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from jarvis.core.errors import IntegrityError
from jarvis.core.hashing import canonical_json
from jarvis.core.types import Nanos
from jarvis.ingest.fetch import RawStatus

FETCH_LOG_SCHEMA_VERSION = 1

_HOUR_KEY_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True, slots=True)
class FetchLogEntry:
    hour_utc_ns: Nanos
    status: RawStatus  # fetched | empty | missing | skipped_existing | rate_limited
    attempts: int
    byte_count: int
    recorded_utc: str
    error: str | None


def _month_of(hour_utc_ns: Nanos) -> tuple[int, int]:
    dt = datetime.fromtimestamp(hour_utc_ns // 1_000_000_000, tz=timezone.utc)
    return dt.year, dt.month


def _path_for_month(repo_root: Path, instrument: str, year: int, month: int) -> Path:
    return (
        repo_root
        / "data"
        / "raw"
        / "ticks"
        / instrument
        / "_fetch_log"
        / f"{year:04d}-{month:02d}.json"
    )


def _hour_key(hour_utc_ns: Nanos) -> str:
    dt = datetime.fromtimestamp(hour_utc_ns // 1_000_000_000, tz=timezone.utc)
    return dt.strftime(_HOUR_KEY_FORMAT)


def _parse_hour_key(key: str) -> Nanos:
    dt = datetime.strptime(key, _HOUR_KEY_FORMAT).replace(tzinfo=timezone.utc)
    return Nanos(int(dt.timestamp()) * 1_000_000_000)


def fetch_log_path(repo_root: Path, instrument: str, hour_utc_ns: Nanos) -> Path:
    """data/raw/ticks/{instrument}/_fetch_log/{YYYY-MM}.json for the month
    containing this hour. Plain UTC calendar arithmetic, as in urls.py --
    do not import timeengine (see module docstring)."""
    year, month = _month_of(hour_utc_ns)
    return _path_for_month(repo_root, instrument, year, month)


def read_fetch_log(
    repo_root: Path, instrument: str, year: int, month: int
) -> dict[Nanos, FetchLogEntry]:
    """Read one month's log. Returns an empty dict if the file does not
    exist -- an absent log is a valid state meaning "nothing fetched for
    this month yet", not an error. Raises IntegrityError if the file exists
    but is malformed or carries an unknown schema_version: a corrupt
    provenance record must never be silently treated as an absent one."""
    path = _path_for_month(repo_root, instrument, year, month)
    if not path.is_file():
        return {}

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"fetch log {path} is malformed: {exc}") from exc

    if not isinstance(raw, dict) or "schema_version" not in raw or "hours" not in raw:
        raise IntegrityError(
            f"fetch log {path} is malformed: missing required top-level fields"
        )
    if raw["schema_version"] != FETCH_LOG_SCHEMA_VERSION:
        raise IntegrityError(
            f"fetch log {path} has unknown schema_version {raw['schema_version']!r}; "
            f"expected {FETCH_LOG_SCHEMA_VERSION}"
        )
    if not isinstance(raw["hours"], dict):
        raise IntegrityError(f"fetch log {path} is malformed: 'hours' is not a mapping")

    entries: dict[Nanos, FetchLogEntry] = {}
    try:
        for key, body in raw["hours"].items():
            hour_ns = _parse_hour_key(key)
            entries[hour_ns] = FetchLogEntry(
                hour_utc_ns=hour_ns,
                status=body["status"],
                attempts=body["attempts"],
                byte_count=body["byte_count"],
                recorded_utc=body["recorded_utc"],
                error=body.get("error"),
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise IntegrityError(f"fetch log {path} is malformed: {exc}") from exc

    return entries


def merge_fetch_log(repo_root: Path, instrument: str, entries: Sequence[FetchLogEntry]) -> None:
    """Merge entries into the appropriate monthly logs, one write per month
    touched. Existing entries for the same hour are OVERWRITTEN -- a later
    fetch supersedes an earlier one, so a retried "missing" hour that later
    succeeds correctly ends up "fetched".

    Writes are ATOMIC: serialised to a temp file in the same directory,
    then os.replace() onto the target. A partially-written provenance file
    is worse than none.

    Entries with status "skipped_existing" are NOT written: that status
    means a blob already existed and we did not go to the network. Writing
    it would overwrite the real provenance from the run that actually
    fetched the hour."""
    by_month: dict[tuple[int, int], list[FetchLogEntry]] = {}
    for entry in entries:
        if entry.status == "skipped_existing":
            continue
        by_month.setdefault(_month_of(entry.hour_utc_ns), []).append(entry)

    for (year, month), month_entries in by_month.items():
        existing = read_fetch_log(repo_root, instrument, year, month)
        for entry in month_entries:
            existing[entry.hour_utc_ns] = entry

        hours_payload = {
            _hour_key(hour_ns): {
                "status": e.status,
                "attempts": e.attempts,
                "byte_count": e.byte_count,
                "recorded_utc": e.recorded_utc,
                "error": e.error,
            }
            for hour_ns, e in sorted(existing.items())
        }
        payload = {
            "instrument": instrument,
            "month": f"{year:04d}-{month:02d}",
            "schema_version": FETCH_LOG_SCHEMA_VERSION,
            "hours": hours_payload,
        }

        path = _path_for_month(repo_root, instrument, year, month)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(path.name + ".tmp")
        try:
            tmp_path.write_text(canonical_json(payload), encoding="utf-8")
            os.replace(tmp_path, path)
        except OSError as exc:
            tmp_path.unlink(missing_ok=True)
            raise IntegrityError(f"failed to write fetch log {path}: {exc}") from exc
