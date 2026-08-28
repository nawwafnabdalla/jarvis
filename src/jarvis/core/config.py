"""Repository-root discovery and configuration loading."""

from datetime import date, timedelta
from pathlib import Path

import yaml

from jarvis.core.errors import ConfigError
from jarvis.core.types import Instrument

_REQUIRED_PERIODS = ("development", "validation", "holdout")


def repo_root() -> Path:
    """Walk upward from this file until a directory containing pyproject.toml
    is found. Raises ConfigError if none. Uses pathlib exclusively."""
    current = Path(__file__).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise ConfigError("could not locate repo root: no pyproject.toml found in any parent directory")


def load_yaml(path: Path) -> dict:
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if data is None:
        raise ConfigError(f"config file is empty: {path}")
    if not isinstance(data, dict):
        raise ConfigError(f"config file does not contain a mapping at top level: {path}")
    return data


def load_instruments() -> dict[str, Instrument]:
    """Load config/instruments.yaml. Raises ConfigError if it contains any
    symbol other than GBPUSD (frozen decision F-01)."""
    path = repo_root() / "config" / "instruments.yaml"
    data = load_yaml(path)
    if list(data.keys()) != ["GBPUSD"]:
        raise ConfigError(
            "config/instruments.yaml must contain exactly one instrument, GBPUSD "
            f"(frozen decision F-01); found: {sorted(data.keys())}"
        )
    entry = data["GBPUSD"]
    instrument = Instrument(
        symbol="GBPUSD",
        point_scale=float(entry["point_scale"]),
        digits=int(entry["digits"]),
    )
    return {"GBPUSD": instrument}


def load_periods() -> dict[str, tuple[date, date | None]]:
    """Load config/periods.yaml. Validates that the three periods are
    contiguous and non-overlapping. Raises ConfigError otherwise."""
    path = repo_root() / "config" / "periods.yaml"
    data = load_yaml(path)

    for name in _REQUIRED_PERIODS:
        if name not in data:
            raise ConfigError(f"config/periods.yaml is missing required period: {name}")

    periods: dict[str, tuple[date, date | None]] = {}
    for name in _REQUIRED_PERIODS:
        entry = data[name]
        start, end = entry["start"], entry["end"]
        if not isinstance(start, date):
            raise ConfigError(f"period {name!r} has a non-date start: {start!r}")
        if end is not None and not isinstance(end, date):
            raise ConfigError(f"period {name!r} has a non-date end: {end!r}")
        periods[name] = (start, end)

    dev_start, dev_end = periods["development"]
    val_start, val_end = periods["validation"]
    hold_start, hold_end = periods["holdout"]

    if dev_end is None or val_end is None:
        raise ConfigError("development and validation periods must both have an end date")
    if hold_end is not None:
        raise ConfigError("holdout period must be open-ended (end: null)")

    if val_start != dev_end + timedelta(days=1):
        raise ConfigError(
            f"gap or overlap between development (ends {dev_end}) and validation "
            f"(starts {val_start}); periods must be contiguous and non-overlapping"
        )
    if hold_start != val_end + timedelta(days=1):
        raise ConfigError(
            f"gap or overlap between validation (ends {val_end}) and holdout "
            f"(starts {hold_start}); periods must be contiguous and non-overlapping"
        )

    return periods
