from datetime import datetime, timezone
from pathlib import Path

import pytest

from jarvis.core.errors import UserError
from jarvis.core.types import Nanos
from jarvis.ingest.urls import dukascopy_url, raw_blob_path


def _ns(y: int, m: int, d: int, h: int = 0) -> Nanos:
    return Nanos(int(datetime(y, m, d, h, tzinfo=timezone.utc).timestamp()) * 1_000_000_000)


def test_url_january_is_zero_indexed():
    url = dukascopy_url("GBPUSD", _ns(2024, 1, 15, 3))
    assert url == "https://datafeed.dukascopy.com/datafeed/GBPUSD/2024/00/15/03h_ticks.bi5"


def test_url_december_is_eleven():
    url = dukascopy_url("GBPUSD", _ns(2024, 12, 15, 3))
    assert url == "https://datafeed.dukascopy.com/datafeed/GBPUSD/2024/11/15/03h_ticks.bi5"


def test_url_rejects_non_hour_aligned_timestamp():
    with pytest.raises(UserError):
        dukascopy_url("GBPUSD", Nanos(_ns(2024, 1, 15, 3) + 1))


def test_raw_blob_path_uses_one_indexed_calendar_values(tmp_path: Path):
    path = raw_blob_path(tmp_path, "GBPUSD", _ns(2024, 1, 15, 3))
    assert path == (
        tmp_path / "data" / "raw" / "ticks" / "GBPUSD" / "2024" / "01" / "15" / "03h_ticks.bi5"
    )
