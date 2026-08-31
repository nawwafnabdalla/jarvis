"""Shared synthetic bar-frame fixture builders for the features test
suite (test_features_library.py, test_features_leakage.py).

Deliberately a PLAIN module, not a test module, and test modules must
never import fixtures from one another -- see the WP-007 follow-up this
file resolves. `test_features_leakage.py` previously did
`from tests.unit.test_features_library import (...)`, which only
resolves under `python -m pytest` (CWD on sys.path) and not under a bare
`pytest` invocation (no such insertion, and tests/ deliberately has no
__init__.py to make it a real package). That made leakage-harness
collection depend on how the test runner was invoked -- for the single
most important guard in this package, a collection error some CI setups
report as "0 tests collected" rather than a failure is a worse outcome
than a normal test failure.

Both consuming test files import this as a plain top-level module
(`from _feature_fixtures import ...`), which resolves under both
invocation forms because pytest's rootdir insertion puts tests/unit
itself on sys.path when there is no __init__.py there. Do not add an
__init__.py to tests/ to "fix" this a different way -- that changes
pytest's import mode for the entire suite, a far larger blast radius
than this file's problem justifies.
"""

from datetime import date, datetime, timedelta, timezone

import numpy as np
import polars as pl

from jarvis.bars import BAR_SCHEMA
from jarvis.core.types import Nanos
from jarvis.sessions import load_session_set

NS_PER_MINUTE = 60_000_000_000
NS_PER_HOUR = 3_600_000_000_000
GAP_TOLERANCE_NS = 5 * 60 * 1_000_000_000

SESSION_SET = load_session_set("fx_core", 1)


def ns(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> Nanos:
    return Nanos(int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp()) * 1_000_000_000)


def row(
    ts_ns: int,
    *,
    bid_h,
    bid_l,
    bid_c,
    ask_c=None,
    ask_h=None,
    ask_l=None,
    prev_gap_ns=None,
) -> dict:
    ask_c = bid_c + 0.0002 if ask_c is None else ask_c
    ask_h = ask_c + 0.0001 if ask_h is None else ask_h
    ask_l = ask_c - 0.0001 if ask_l is None else ask_l
    return {
        "ts_utc_ns": ts_ns,
        "bid_o": bid_c,
        "bid_h": bid_h,
        "bid_l": bid_l,
        "bid_c": bid_c,
        "ask_o": ask_c,
        "ask_h": ask_h,
        "ask_l": ask_l,
        "ask_c": ask_c,
        "tick_count": 1,
        "first_tick_ns": ts_ns,
        "last_tick_ns": ts_ns,
        "spread_open": ask_c - bid_c,
        "spread_max": ask_h - bid_l,
        "spread_twa": ask_c - bid_c,
        "prev_gap_ns": prev_gap_ns,
    }


def frame(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=BAR_SCHEMA)


def build_fixture_bars(seed: int = 1) -> pl.DataFrame:
    """3 trading days of 1-minute bars (2024-01-15 through 2024-01-17,
    Mon-Wed, no DST in play for London or NY in January) with a realistic
    pre_london window (00:00-08:00 UTC == 00:00-08:00 London in winter),
    one absent-minute run (5 minutes on day 1), and one multi-hour gap (3
    hours on day 2)."""
    start = ns(2024, 1, 15, 0, 0)
    end = ns(2024, 1, 18, 0, 0)
    gap1 = (ns(2024, 1, 15, 10, 0), ns(2024, 1, 15, 10, 5))
    gap2 = (ns(2024, 1, 16, 12, 0), ns(2024, 1, 16, 15, 0))

    rng = np.random.default_rng(seed)
    rows: list[dict] = []
    prev_ts: int | None = None
    ts = start
    price = 1.1000
    while ts < end:
        if (gap1[0] <= ts < gap1[1]) or (gap2[0] <= ts < gap2[1]):
            ts += NS_PER_MINUTE
            continue
        price += float(rng.normal(0, 0.00005))
        bid_c = price
        ask_c = price + 0.0002
        bid_h = max(bid_c, bid_c + abs(float(rng.normal(0, 0.00002))))
        bid_l = min(bid_c, bid_c - abs(float(rng.normal(0, 0.00002))))
        ask_h = ask_c + abs(float(rng.normal(0, 0.00002)))
        ask_l = ask_c - abs(float(rng.normal(0, 0.00002)))
        prev_gap_ns = None if prev_ts is None else ts - prev_ts
        rows.append(
            {
                "ts_utc_ns": ts,
                "bid_o": bid_c,
                "bid_h": bid_h,
                "bid_l": bid_l,
                "bid_c": bid_c,
                "ask_o": ask_c,
                "ask_h": ask_h,
                "ask_l": ask_l,
                "ask_c": ask_c,
                "tick_count": 1,
                "first_tick_ns": ts,
                "last_tick_ns": ts,
                "spread_open": ask_c - bid_c,
                "spread_max": ask_h - bid_l,
                "spread_twa": ask_c - bid_c,
                "prev_gap_ns": prev_gap_ns,
            }
        )
        prev_ts = ts
        ts += NS_PER_MINUTE

    return pl.DataFrame(rows, schema=BAR_SCHEMA)


def multi_day_pre_london(day_ranges: list[tuple[date, float]]) -> pl.DataFrame:
    """One day per (date, range) pair: two bars inside the pre_london
    window (00:00 and 00:01 UTC) whose mid values are exactly `range`
    apart, giving that day a precisely-controlled pre_london_range."""
    rows = []
    for day, rng in day_ranges:
        day_start = (
            int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
            * 1_000_000_000
        )
        base = 1.1000
        rows.append(row(Nanos(day_start), bid_h=base, bid_l=base, bid_c=base, ask_c=base))
        rows.append(
            row(
                Nanos(day_start + NS_PER_MINUTE),
                bid_h=base + rng,
                bid_l=base + rng,
                bid_c=base + rng,
                ask_c=base + rng,
            )
        )
        # One bar after window close so the day's value is observable.
        rows.append(
            row(
                Nanos(day_start + 8 * 60 * NS_PER_MINUTE),
                bid_h=base,
                bid_l=base,
                bid_c=base,
                ask_c=base,
            )
        )
    return frame(rows)


def weekdays_from(start: date, n: int) -> list[date]:
    days = []
    d = start
    while len(days) < n:
        if d.isoweekday() <= 5:
            days.append(d)
        d = d + timedelta(days=1)
    return days
