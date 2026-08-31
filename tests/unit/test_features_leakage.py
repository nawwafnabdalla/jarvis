"""The auto-generated leakage harness (Technical Bible Part F §F.4 / WP-007).

Parameterised over jarvis.features.base.REGISTRY: registering a new
feature automatically registers its leakage tests here, with no
per-feature code required. This is the most important deliverable in
WP-007 -- a future feature that reads the future must fail without
anyone remembering to write a test for it.

Checks (per feature):
  L-1  truncation invariance
  L-2  shuffled-future invariance
  L-3  session_terminal nulling (session_terminal features only)
  L-4  warmup nulling
  L-5  determinism

test_harness_catches_deliberately_leaky_feature (acceptance criterion 2)
registers a test-only feature that reads one bar ahead, proves L-1
catches it, then removes it -- see that test's docstring.
"""

from datetime import date

import numpy as np
import polars as pl
import pytest

from jarvis.core.types import Nanos
from jarvis.features import REGISTRY, FeatureDef, LookbackSpec, compute, register

# Plain-module import (not a test-to-test import) -- see _feature_fixtures's
# own docstring for why this must not be `from tests.unit._feature_fixtures`
# or `from tests.unit.test_features_library import ...` (the latter is what
# broke collection under a bare `pytest` invocation in the first place).
from _feature_fixtures import (
    NS_PER_MINUTE,
    SESSION_SET as _SESSION_SET,
    build_fixture_bars,
    frame as _frame,
    multi_day_pre_london as _multi_day_pre_london,
    ns as _ns,
    row as _row,
    weekdays_from as _weekdays_from,
)

_FEATURE_NAMES = sorted(REGISTRY.keys())
_TRUNCATION_K = 2000  # comfortably < fixture height - 50, and >= atr_bars' n=1440


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fixture_bars() -> pl.DataFrame:
    return build_fixture_bars()


def _shuffle_future(bars: pl.DataFrame, k: int, seed: int = 99) -> pl.DataFrame:
    """Bars at index <= k are untouched. Rows are NEVER reordered --
    ts_utc_ns stays intact and strictly ascending, since compute() and
    every feature here legitimately rely on it (trading_day_boundaries's
    searchsorted bucketing, for one); reordering rows would test
    behaviour on malformed input, not leakage, and any resulting failure
    would be a phantom unrelated to the property under test.

    Only the PRICE columns (bid/ask O/H/L/C) are randomised, for rows
    with index > k, plus the derived spread_* columns regenerated from
    them so the perturbed bars stay internally consistent. tick_count,
    first_tick_ns, last_tick_ns and prev_gap_ns are left untouched --
    perturbing prev_gap_ns would change rv_60m's null mask for a reason
    unrelated to leakage and muddy the signal.

    The property under test is unchanged: no value at index <= k may
    depend on any price at index > k. This holds even for
    session_terminal features across the k boundary -- if k falls inside
    a still-open session window, values at <= k are null and stay null;
    if k falls after that window's close, the day's terminal value was
    already fixed by bars at or before the window's end, all of which are
    at or before k or otherwise untouched here."""
    n = bars.height
    if k + 1 >= n:
        return bars

    rng = np.random.default_rng(seed)
    head = bars[: k + 1]
    tail = bars[k + 1 :]

    noise = rng.normal(0, 0.001, size=tail.height)
    price_cols = ["bid_o", "bid_h", "bid_l", "bid_c", "ask_o", "ask_h", "ask_l", "ask_c"]
    perturbed_tail = tail.with_columns(
        [(pl.col(c) + pl.Series(noise)).alias(c) for c in price_cols]
    )
    perturbed_tail = perturbed_tail.with_columns(
        [
            (pl.col("ask_o") - pl.col("bid_o")).alias("spread_open"),
            pl.max_horizontal(pl.col("ask_h") - pl.col("bid_l"), pl.col("ask_o") - pl.col("bid_o")).alias(
                "spread_max"
            ),
            (pl.col("ask_c") - pl.col("bid_c")).alias("spread_twa"),
        ]
    )

    return pl.concat([head, perturbed_tail], how="vertical")


# ---------------------------------------------------------------------------
# Reusable check functions (also used directly by the deliberately-leaky-
# feature verification below)
# ---------------------------------------------------------------------------


def _l1_holds(feature_name: str, bars: pl.DataFrame, k: int) -> bool:
    short = compute([feature_name], bars[:k], _SESSION_SET).frame[feature_name].to_list()
    long_ = compute([feature_name], bars[: k + 50], _SESSION_SET).frame[feature_name].to_list()
    return short == long_[:k]


def _l2_holds(feature_name: str, bars: pl.DataFrame, k: int) -> bool:
    baseline = compute([feature_name], bars, _SESSION_SET).frame[feature_name].to_list()
    shuffled = _shuffle_future(bars, k)
    perturbed = compute([feature_name], shuffled, _SESSION_SET).frame[feature_name].to_list()
    return baseline[: k + 1] == perturbed[: k + 1]


# ---------------------------------------------------------------------------
# L-1: truncation invariance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("feature_name", _FEATURE_NAMES)
def test_l1_truncation_invariance(feature_name: str, fixture_bars: pl.DataFrame):
    assert _l1_holds(feature_name, fixture_bars, _TRUNCATION_K), (
        f"{feature_name} failed L-1 truncation invariance: a value in the "
        f"first {_TRUNCATION_K} bars changed when 50 more bars were appended"
    )


# ---------------------------------------------------------------------------
# L-2: shuffled-future invariance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("feature_name", _FEATURE_NAMES)
def test_l2_shuffled_future_invariance(feature_name: str, fixture_bars: pl.DataFrame):
    assert _l2_holds(feature_name, fixture_bars, _TRUNCATION_K), (
        f"{feature_name} failed L-2: shuffling+perturbing bars after index "
        f"{_TRUNCATION_K} changed a value at or before that index"
    )


# ---------------------------------------------------------------------------
# L-3: session_terminal nulling
# ---------------------------------------------------------------------------


_SESSION_TERMINAL_NAMES = sorted(
    name for name, defn in REGISTRY.items() if defn.leakage_class == "session_terminal"
)


@pytest.mark.parametrize("feature_name", _SESSION_TERMINAL_NAMES)
def test_l3_session_terminal_nulling(feature_name: str, fixture_bars: pl.DataFrame):
    from jarvis.features.base import session_window_bounds, trading_day_boundaries

    defn = REGISTRY[feature_name]
    session_name = str(defn.params["session"])

    result = compute([feature_name], fixture_bars, _SESSION_SET).frame

    days, day_idx = trading_day_boundaries(fixture_bars)
    _starts, ends = session_window_bounds(_SESSION_SET, session_name, days)
    ts = fixture_bars["ts_utc_ns"].to_numpy()
    window_end = ends[day_idx]
    before_close = ts < window_end

    before_df = result.filter(pl.Series(before_close))
    assert before_df.height > 0  # the fixture must actually cover a pre-close bar
    assert before_df[feature_name].null_count() == before_df.height, (
        f"{feature_name} is non-null before its session window closed on at "
        "least one bar in the fixture"
    )


# ---------------------------------------------------------------------------
# L-4: warmup nulling
# ---------------------------------------------------------------------------


def _generic_bars(count: int) -> pl.DataFrame:
    if count <= 0:
        return _frame([])
    rows = []
    price = 1.10
    ts0 = _ns(2024, 1, 9, 0, 0)  # an ordinary Tuesday
    for i in range(count):
        ts = Nanos(ts0 + i * NS_PER_MINUTE)
        price += 0.00001 * ((-1) ** i)
        rows.append(
            _row(
                ts,
                bid_h=price + 0.0002,
                bid_l=price - 0.0002,
                bid_c=price,
                prev_gap_ns=(None if i == 0 else NS_PER_MINUTE),
            )
        )
    return _frame(rows)


def _sessions_warmup_fixture(defn: FeatureDef, count: int) -> pl.DataFrame:
    """`count` sessions' worth of CLOSED windows -- for our features count
    is always 0 (n=1 lookback), meaning "no closed pre_london window at
    all": every bar in the fixture falls strictly before window close."""
    session_name = str(defn.params.get("session", "pre_london"))
    day = date(2024, 1, 15)
    window = _SESSION_SET.window(session_name, day)
    if count > 0:
        raise NotImplementedError("no library feature currently needs sessions lookback n > 1")
    rows = [
        _row(Nanos(window.start_ns + i * NS_PER_MINUTE), bid_h=1.1002, bid_l=1.0998, bid_c=1.1000)
        for i in range(5)
    ]
    return _frame(rows)


def _trading_days_warmup_fixture(count: int) -> pl.DataFrame:
    """`count` prior eligible trading days plus one more day (today) that
    -- with fewer than n prior eligible days available -- must be null."""
    days = _weekdays_from(date(2024, 1, 1), count + 1)
    day_ranges = [(d, 0.0010 + 0.00001 * i) for i, d in enumerate(days[:count])]
    day_ranges.append((days[count], 0.005))
    return _multi_day_pre_london(day_ranges)


def _l4_fixture(defn: FeatureDef) -> pl.DataFrame:
    count = defn.lookback.n - 1
    if defn.lookback.unit == "bars":
        return _generic_bars(count)
    if defn.lookback.unit == "sessions":
        return _sessions_warmup_fixture(defn, count)
    if defn.lookback.unit == "trading_days":
        return _trading_days_warmup_fixture(count)
    raise AssertionError(f"unknown lookback unit: {defn.lookback.unit!r}")


@pytest.mark.parametrize("feature_name", _FEATURE_NAMES)
def test_l4_warmup_nulling(feature_name: str):
    defn = REGISTRY[feature_name]
    bars = _l4_fixture(defn)
    result = compute([feature_name], bars, _SESSION_SET).frame
    assert result[feature_name].null_count() == result.height, (
        f"{feature_name} produced a non-null value with only "
        f"lookback.n - 1 = {defn.lookback.n - 1} {defn.lookback.unit} of history"
    )


# ---------------------------------------------------------------------------
# L-5: determinism
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("feature_name", _FEATURE_NAMES)
def test_l5_determinism(feature_name: str, fixture_bars: pl.DataFrame):
    a = compute([feature_name], fixture_bars, _SESSION_SET).frame[feature_name].to_list()
    b = compute([feature_name], fixture_bars, _SESSION_SET).frame[feature_name].to_list()
    assert a == b


# ---------------------------------------------------------------------------
# Acceptance criterion 2: prove the harness actually catches a leak
# ---------------------------------------------------------------------------


def _leaky_one_bar_ahead_compute(ctx):
    """Deliberately leaky, test-only: value[i] is tomorrow's bid_c, one
    bar ahead of what should be visible at bar i."""
    bid_c = ctx.bars["bid_c"]
    shifted = bid_c.shift(-1)
    return shifted.fill_null(bid_c)


def test_harness_catches_deliberately_leaky_feature(fixture_bars: pl.DataFrame):
    """Registers a feature whose compute() reads one bar into the future
    (value[i] = bars[i+1]'s bid_c), runs the same L-1 check the
    parameterised suite above runs for every real feature, and asserts it
    FAILS -- proving the harness is not a rubber stamp. The feature is
    removed from REGISTRY in a finally block regardless of outcome, so it
    never appears in the parameterised suites above (which read REGISTRY
    at module-import time, before this test runs, so it could not anyway
    -- this is belt and braces).

    This is the check required by WP-007 acceptance criterion 2. Reported
    in closing notes as instructed."""
    name = "_test_deliberately_leaky_one_bar_ahead"
    assert name not in REGISTRY
    try:
        register(
            FeatureDef(
                name=name,
                version=1,
                dtype=pl.Float64,
                lookback=LookbackSpec("bars", 1),
                gap_tolerance_ns=None,
                requires=(),
                params={},
                leakage_class="causal",
                compute=_leaky_one_bar_ahead_compute,
            )
        )
        holds = _l1_holds(name, fixture_bars, _TRUNCATION_K)
        assert not holds, (
            "the leakage harness FAILED to catch a deliberately-injected "
            "one-bar-ahead leak via L-1 truncation invariance -- the harness "
            "itself is broken"
        )
    finally:
        REGISTRY.pop(name, None)
