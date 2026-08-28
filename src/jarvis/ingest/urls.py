"""Dukascopy .bi5 URL construction and raw-blob filesystem paths."""

from datetime import datetime, timezone
from pathlib import Path

from jarvis.core.errors import UserError
from jarvis.core.types import Nanos

NS_PER_HOUR = 3_600_000_000_000

_URL_TEMPLATE = (
    "https://datafeed.dukascopy.com/datafeed/{instrument}/{y:04d}/{m0:02d}/{d:02d}/{h:02d}h_ticks.bi5"
)


def _require_hour_aligned(hour_utc_ns: Nanos) -> None:
    if hour_utc_ns % NS_PER_HOUR != 0:
        raise UserError(f"hour_utc_ns must be exactly on an hour boundary, got {hour_utc_ns}")


def dukascopy_url(instrument: str, hour_utc_ns: Nanos) -> str:
    """Build the Dukascopy .bi5 URL for one UTC hour.

    URL shape:
        https://datafeed.dukascopy.com/datafeed/{instrument}/{Y}/{M0:02d}/{D:02d}/{H:02d}h_ticks.bi5

    CRITICAL: the month component is ZERO-INDEXED (January = 00, December = 11).
    This is the single most common Dukascopy integration bug. hour_utc_ns must
    decompose using plain UTC calendar arithmetic (datetime.fromtimestamp with
    tz=timezone.utc, or equivalent) — no timezone conversion, since Dukascopy's
    hours already are UTC. Raises UserError if hour_utc_ns is not exactly on
    an hour boundary (hour_utc_ns % 3_600_000_000_000 != 0).
    """
    _require_hour_aligned(hour_utc_ns)
    dt = datetime.fromtimestamp(hour_utc_ns // 1_000_000_000, tz=timezone.utc)
    return _URL_TEMPLATE.format(
        instrument=instrument,
        y=dt.year,
        m0=dt.month - 1,
        d=dt.day,
        h=dt.hour,
    )


def raw_blob_path(repo_root: Path, instrument: str, hour_utc_ns: Nanos) -> Path:
    """Return data/raw/ticks/{instrument}/{YYYY}/{MM}/{DD}/{HH}h_ticks.bi5
    under repo_root, using pathlib exclusively. MM and DD here are the
    ordinary 1-indexed calendar values for the directory structure — the
    zero-indexing is a Dukascopy URL quirk only, not a filesystem convention.
    Do not propagate the zero-indexing into the directory layout."""
    dt = datetime.fromtimestamp(hour_utc_ns // 1_000_000_000, tz=timezone.utc)
    return (
        repo_root
        / "data"
        / "raw"
        / "ticks"
        / instrument
        / f"{dt.year:04d}"
        / f"{dt.month:02d}"
        / f"{dt.day:02d}"
        / f"{dt.hour:02d}h_ticks.bi5"
    )
