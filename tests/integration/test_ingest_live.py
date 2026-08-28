"""Live integration test against the real Dukascopy endpoint.

Excluded from default `pytest` runs by the `live` marker (see pyproject.toml
addopts); run explicitly with `pytest tests/integration/test_ingest_live.py
-v -m live`.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from jarvis.core.types import Nanos
from jarvis.ingest.fetch import ingest_range

pytestmark = pytest.mark.live


def _ns(y: int, m: int, d: int, h: int = 0) -> Nanos:
    return Nanos(int(datetime(y, m, d, h, tzinfo=timezone.utc).timestamp()) * 1_000_000_000)


def test_fetch_one_known_week_against_real_dukascopy(tmp_path: Path):
    start = _ns(2024, 1, 1)
    end = _ns(2024, 1, 8)

    report = ingest_range(tmp_path, "GBPUSD", start, end)

    assert report.hours_expected == 168
    assert report.hours_fetched + report.hours_empty + report.hours_missing == 168
    print(report)
