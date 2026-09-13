"""R5 -- Spread and cost climate (Technical Bible Part 2 §G.1.2).

Question: what does it actually cost to transact, by hour and day?
Calculation: `spread_twa` distribution by hour-of-week; by year; ratio of
median spread to median 60-minute range for the same hour.
Uncertainty: quantiles with bootstrap CIs.

Pure computation over an already-loaded bars frame -- this module never
reads `data/` itself (matching `jarvis.features.compute`'s own pattern),
so it needs no exception to the single-reader rule. Needs bars alone: no
feature computation, no session set.
"""

from dataclasses import dataclass

import numpy as np
import polars as pl

from jarvis.core.bootstrap import BootstrapCI, bootstrap_ci
from jarvis.core.types import Nanos

# ISO weekday numbering (polars' own dt.weekday(): Monday=1 .. Sunday=7),
# in Europe/London local time -- consistent with this codebase's existing
# hour-of-day-London convention (feature #19, hour_of_day_london) rather
# than UTC, since every other hour-anchored concept in this project
# (session windows, the pre_london/london/new_york sessions themselves)
# is already London-anchored. "By year" uses the UTC calendar year of
# ts_utc_ns, matching config/periods.yaml's own UTC-dated period
# boundaries -- the two conventions rarely disagree except right at a
# year boundary, and nothing here claims sub-hour precision on that edge.
_LONDON_TZ = "Europe/London"

_MIN_BOOTSTRAP_N = 10  # below this, no CI is attempted at all -- G.1.3's
# n<10 suppression makes any such CI unreportable anyway, and a 2000-
# resample bootstrap on fewer than 10 points is not a meaningful estimate.

_BOOTSTRAP_SEED = 20260913  # fixed: reproducibility (Part 2 §F.1) applies
# to this report the same way it applies to a feature's recomputation.

# core.bootstrap's own default (2000) is sound for typical sample sizes,
# but R5's year buckets are real, ~350,000-observation samples (confirmed
# by running this against the actual 2007-2014 dataset) -- a percentile
# bootstrap's cost scales with n_resamples * len(values) regardless of
# memory-safe chunking, and 2000 resamples at that scale measured ~45s
# PER bucket, ~8 minutes total across every hour-of-week and year bucket.
# 500 resamples (still within the commonly-cited 500-2000 range for a
# percentile-method 95% CI, and R5 is explicitly exploratory, not
# confirmatory, per its own DESCRIPTIVE watermark) measured ~11s per
# large bucket -- a disclosed precision/time trade-off, not a silent one.
_N_RESAMPLES = 500


@dataclass(frozen=True, slots=True)
class HourOfWeekRow:
    weekday: int  # 1=Monday .. 7=Sunday (ISO), Europe/London local
    hour: int  # 0-23, Europe/London local
    n: int
    spread_ci: BootstrapCI | None  # None if n < _MIN_BOOTSTRAP_N
    range_60m_ci: BootstrapCI | None  # None if n < _MIN_BOOTSTRAP_N
    spread_to_range_ratio: float | None  # median(spread_twa) / median(range_60m)


@dataclass(frozen=True, slots=True)
class YearRow:
    year: int
    n: int
    spread_ci: BootstrapCI | None  # None if n < _MIN_BOOTSTRAP_N


@dataclass(frozen=True, slots=True)
class R5Result:
    start_ns: Nanos
    end_ns: Nanos
    bars_examined: int
    by_hour_of_week: tuple[HourOfWeekRow, ...]
    by_year: tuple[YearRow, ...]


def _add_derived_columns(bars: pl.DataFrame) -> pl.DataFrame:
    mid = (pl.col("bid_c") + pl.col("ask_c")) / 2.0
    dt_utc = pl.from_epoch(pl.col("ts_utc_ns"), time_unit="ns").dt.replace_time_zone("UTC")
    dt_london = dt_utc.dt.convert_time_zone(_LONDON_TZ)
    return bars.with_columns(
        mid.alias("_mid"),
        dt_utc.dt.year().alias("_year"),
        dt_london.dt.weekday().alias("_weekday"),
        dt_london.dt.hour().alias("_hour"),
    ).with_columns(
        (
            pl.col("_mid").rolling_max(window_size=60, min_samples=60)
            - pl.col("_mid").rolling_min(window_size=60, min_samples=60)
        ).alias("_range_60m")
    )


def _row_bootstrap(values: np.ndarray, *, seed: int) -> BootstrapCI | None:
    if values.size < _MIN_BOOTSTRAP_N:
        return None
    return bootstrap_ci(values, seed=seed, n_resamples=_N_RESAMPLES)


def compute_r5(bars: pl.DataFrame, *, start_ns: Nanos, end_ns: Nanos) -> R5Result:
    """Compute R5 over `bars`, which the caller must have already loaded
    for exactly `[start_ns, end_ns)` (see `describe.periods.
    stage2_descriptive_range`) -- this function does not re-filter by
    range itself, since it has no way to read `data/` to verify what it
    was handed actually matches, and trusts its caller the same way
    `jarvis.features.compute` trusts the bars frame it is given."""
    if bars.height == 0:
        return R5Result(
            start_ns=start_ns, end_ns=end_ns, bars_examined=0, by_hour_of_week=(), by_year=()
        )

    enriched = _add_derived_columns(bars).filter(pl.col("spread_twa").is_not_null())

    by_hour_of_week: list[HourOfWeekRow] = []
    grouped_how = enriched.group_by(["_weekday", "_hour"], maintain_order=True).agg(
        pl.col("spread_twa").alias("_spreads"),
        pl.col("_range_60m").drop_nulls().alias("_ranges"),
    )
    for row in grouped_how.iter_rows(named=True):
        weekday, hour = int(row["_weekday"]), int(row["_hour"])
        spreads = np.asarray(row["_spreads"], dtype=np.float64)
        ranges = np.asarray(row["_ranges"], dtype=np.float64)
        n = spreads.size

        # Seed derived from the bucket's own (weekday, hour) key, not
        # iteration order or a shared constant -- every bucket's bootstrap
        # draws from an independent RNG stream (still fully reproducible),
        # rather than every same-n bucket replaying identical resample
        # indices against different underlying values.
        bucket_seed = _BOOTSTRAP_SEED + weekday * 100 + hour
        spread_ci = _row_bootstrap(spreads, seed=bucket_seed)
        range_ci = _row_bootstrap(ranges, seed=bucket_seed + 10_000)

        ratio = None
        if spread_ci is not None and range_ci is not None and range_ci.point_estimate != 0.0:
            ratio = spread_ci.point_estimate / range_ci.point_estimate

        by_hour_of_week.append(
            HourOfWeekRow(
                weekday=weekday,
                hour=hour,
                n=n,
                spread_ci=spread_ci,
                range_60m_ci=range_ci,
                spread_to_range_ratio=ratio,
            )
        )
    by_hour_of_week.sort(key=lambda r: (r.weekday, r.hour))

    by_year: list[YearRow] = []
    grouped_year = enriched.group_by("_year", maintain_order=True).agg(
        pl.col("spread_twa").alias("_spreads")
    )
    for row in grouped_year.iter_rows(named=True):
        year = int(row["_year"])
        spreads = np.asarray(row["_spreads"], dtype=np.float64)
        by_year.append(
            YearRow(
                year=year,
                n=spreads.size,
                spread_ci=_row_bootstrap(spreads, seed=_BOOTSTRAP_SEED + 20_000 + year),
            )
        )
    by_year.sort(key=lambda r: r.year)

    return R5Result(
        start_ns=start_ns,
        end_ns=end_ns,
        bars_examined=bars.height,
        by_hour_of_week=tuple(by_hour_of_week),
        by_year=tuple(by_year),
    )
