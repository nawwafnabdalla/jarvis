"""Scheduled economic-release calendar (Technical Bible AR-4; Product
Bible Y3, "static curated CSV, versioned as a dataset, exclusion flag
only"; D-014).

A static, one-time-compiled, checked-in file (`config/events/
scheduled_releases.v{n}.csv`, immutable once referenced -- a correction
is a new `v{n+1}` file, never an edit to this one, the same convention
`config/sessions/*.yaml` already uses). This module only reads that file
from disk; it makes no network call, needs no API key, and does nothing
at import time or call time that depends on the outside world (D-014's
offline-by-default requirement).

Purpose, per AR-4: an exclusion flag and a robustness-segmentation input
("does a result survive removing scheduled-event days?"). This module
exposes exactly that lookup and nothing more -- it is never a predictive
feature and is not wired into any report's own computation here (WP-022's
own scope: building and exposing the flag is infrastructure; deciding
whether/how an existing report should use it is a separate, later
decision this module does not make).
"""

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import polars as pl

from jarvis.core.config import repo_root
from jarvis.core.errors import ConfigError

_VALID_EVENT_TYPES = frozenset({"nfp", "fomc", "boe_mpc"})
_REQUIRED_COLUMNS = ("date", "event_type", "source_note")


@dataclass(frozen=True, slots=True)
class ScheduledReleaseCalendar:
    calendar_id: str
    version: int
    by_date: dict[date, tuple[str, ...]]  # date -> sorted, deduplicated event types on that date

    def is_scheduled_release_day(self, d: date) -> bool:
        return d in self.by_date

    def event_types_on(self, d: date) -> tuple[str, ...]:
        """Empty tuple for a day carrying no scheduled release."""
        return self.by_date.get(d, ())


def _csv_path(calendar_id: str, version: int) -> Path:
    return repo_root() / "config" / "events" / f"{calendar_id}.v{version}.csv"


def _validate_schema(frame: pl.DataFrame, *, path: Path) -> None:
    missing = set(_REQUIRED_COLUMNS) - set(frame.columns)
    if missing:
        raise ConfigError(f"{path}: missing required column(s) {sorted(missing)}")
    if frame["date"].null_count() > 0:
        raise ConfigError(f"{path}: 'date' column contains null/unparseable value(s)")
    if frame["event_type"].null_count() > 0:
        raise ConfigError(f"{path}: 'event_type' column contains null value(s)")
    bad_types = set(frame["event_type"].unique().to_list()) - _VALID_EVENT_TYPES
    if bad_types:
        raise ConfigError(f"{path}: unknown event_type value(s) {sorted(bad_types)}; expected one of {sorted(_VALID_EVENT_TYPES)}")


def load_scheduled_releases(calendar_id: str = "scheduled_releases", version: int = 1) -> ScheduledReleaseCalendar:
    """Load `config/events/{calendar_id}.v{version}.csv`, validate its
    schema, and return a queryable calendar. Raises ConfigError on a
    missing file or a schema violation -- mirrors
    `jarvis.sessions.load_session_set_def`'s own versioned-file-loading
    convention (a bad or missing config file is a ConfigError, not a
    silent empty result)."""
    path = _csv_path(calendar_id, version)
    if not path.is_file():
        raise ConfigError(f"scheduled release calendar not found: {path}")

    frame = pl.read_csv(path, schema_overrides={"date": pl.Date})
    _validate_schema(frame, path=path)

    by_date: dict[date, list[str]] = {}
    for row in frame.iter_rows(named=True):
        by_date.setdefault(row["date"], []).append(row["event_type"])

    return ScheduledReleaseCalendar(
        calendar_id=calendar_id,
        version=version,
        by_date={d: tuple(sorted(set(types))) for d, types in by_date.items()},
    )
