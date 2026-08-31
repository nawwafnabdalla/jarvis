from datetime import datetime, timezone

import numpy as np
import polars as pl
import pytest

from jarvis.bars import BAR_SCHEMA
from jarvis.core.errors import UserError
from jarvis.core.types import Nanos
from jarvis.features.base import (
    FeatureDef,
    LookbackSpec,
    apply_session_terminal_mask,
    session_window_bounds,
    trading_day_boundaries,
)
from jarvis.sessions import load_session_set

NS_PER_MINUTE = 60_000_000_000


def _ns(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> Nanos:
    return Nanos(int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp()) * 1_000_000_000)


def _bar_row(ts_ns: int) -> dict:
    return {
        "ts_utc_ns": ts_ns,
        "bid_o": 1.10,
        "bid_h": 1.1002,
        "bid_l": 1.0998,
        "bid_c": 1.1001,
        "ask_o": 1.1003,
        "ask_h": 1.1005,
        "ask_l": 1.1001,
        "ask_c": 1.1004,
        "tick_count": 1,
        "first_tick_ns": ts_ns,
        "last_tick_ns": ts_ns,
        "spread_open": 0.0003,
        "spread_max": 0.0004,
        "spread_twa": 0.00035,
        "prev_gap_ns": None,
    }


def _bars_frame(ts_values: list[int]) -> pl.DataFrame:
    return pl.DataFrame([_bar_row(t) for t in ts_values], schema=BAR_SCHEMA)


# FeatureDef / LookbackSpec construction ------------------------------------


def test_feature_def_constructs_with_expected_fields():
    defn = FeatureDef(
        name="dummy",
        version=1,
        dtype=pl.Float64,
        lookback=LookbackSpec("bars", 10),
        gap_tolerance_ns=None,
        requires=(),
        params={},
        leakage_class="causal",
        compute=lambda ctx: ctx.bars["ts_utc_ns"].cast(pl.Float64),
    )
    assert defn.name == "dummy"
    assert defn.lookback.unit == "bars"
    assert defn.lookback.n == 10


def test_register_rejects_duplicate_name():
    from jarvis.features.base import REGISTRY, register

    defn = FeatureDef(
        name="_test_dup_base",
        version=1,
        dtype=pl.Float64,
        lookback=LookbackSpec("bars", 1),
        gap_tolerance_ns=None,
        requires=(),
        params={},
        leakage_class="causal",
        compute=lambda ctx: ctx.bars["ts_utc_ns"].cast(pl.Float64),
    )
    register(defn)
    try:
        with pytest.raises(UserError):
            register(defn)
    finally:
        del REGISTRY["_test_dup_base"]


# trading_day_boundaries -----------------------------------------------------


def test_trading_day_boundaries_buckets_correctly():
    # 2024-01-15 00:00 UTC and 2024-01-16 00:00 UTC are both well within
    # their respective trading days (2024-01-15, 2024-01-16).
    ts_values = [_ns(2024, 1, 15, 3), _ns(2024, 1, 15, 20), _ns(2024, 1, 16, 3)]
    bars = _bars_frame(ts_values)
    days, day_idx = trading_day_boundaries(bars)

    assert days[day_idx[0]].isoformat() == "2024-01-15"
    assert days[day_idx[1]].isoformat() == "2024-01-15"
    assert days[day_idx[2]].isoformat() == "2024-01-16"


def test_trading_day_boundaries_empty_frame():
    bars = _bars_frame([])
    days, day_idx = trading_day_boundaries(bars)
    assert days == []
    assert len(day_idx) == 0


# session_window_bounds -------------------------------------------------------


def test_session_window_bounds_matches_session_set_window():
    session_set = load_session_set("fx_core", 1)
    from datetime import date

    days = [date(2024, 1, 15), date(2024, 1, 16)]
    starts, ends = session_window_bounds(session_set, "pre_london", days)

    for i, d in enumerate(days):
        window = session_set.window("pre_london", d)
        assert starts[i] == window.start_ns
        assert ends[i] == window.end_ns


# apply_session_terminal_mask -------------------------------------------------


def test_apply_session_terminal_mask_nulls_before_close_and_keeps_after():
    session_set = load_session_set("fx_core", 1)
    window = session_set.window("pre_london", __import__("datetime").date(2024, 1, 15))

    before = Nanos(window.start_ns + NS_PER_MINUTE)
    after = Nanos(window.end_ns + NS_PER_MINUTE)
    bars = _bars_frame([before, after])

    series = pl.Series("dummy", [42.0, 42.0])
    masked = apply_session_terminal_mask(series, bars, session_set, "pre_london")

    assert masked.to_list() == [None, 42.0]


def test_apply_session_terminal_mask_empty_frame_returns_series_unchanged():
    session_set = load_session_set("fx_core", 1)
    bars = _bars_frame([])
    series = pl.Series("dummy", [], dtype=pl.Float64)
    masked = apply_session_terminal_mask(series, bars, session_set, "pre_london")
    assert masked.len() == 0
