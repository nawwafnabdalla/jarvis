from pathlib import Path

import pytest

from jarvis.core.config import load_instruments, load_periods, repo_root
from jarvis.core.errors import ConfigError


def test_repo_root_found():
    root = repo_root()
    assert (root / "pyproject.toml").is_file()


def test_load_instruments_single_gbpusd():
    instruments = load_instruments()
    assert set(instruments) == {"GBPUSD"}
    gbpusd = instruments["GBPUSD"]
    assert gbpusd.symbol == "GBPUSD"
    assert gbpusd.point_scale == 1.0e-5
    assert gbpusd.digits == 5


def test_load_instruments_rejects_second_instrument(isolated_repo: Path):
    config_path = isolated_repo / "config" / "instruments.yaml"
    config_path.write_text(
        "GBPUSD:\n  point_scale: 1.0e-5\n  digits: 5\n"
        "EURUSD:\n  point_scale: 1.0e-4\n  digits: 5\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="F-01"):
        load_instruments()


def test_load_periods_contiguous():
    periods = load_periods()
    assert set(periods) == {"development", "validation", "holdout"}
    assert periods["development"][1] is not None
    assert periods["validation"][1] is not None
    assert periods["holdout"][1] is None


def test_load_periods_rejects_gap(isolated_repo: Path):
    config_path = isolated_repo / "config" / "periods.yaml"
    config_path.write_text(
        "development:\n  start: 2007-01-01\n  end: 2018-12-31\n"
        "validation:\n  start: 2019-01-01\n  end: 2022-12-31\n"
        "holdout:\n  start: 2023-01-02\n  end: null\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_periods()
