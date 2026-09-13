"""Percentile bootstrap confidence intervals (Technical Bible Part 4 milestone
2B: "Bootstrap and interval utilities, shared with Stage 5"). A single,
minimal utility -- not a general-purpose statistics framework -- sized for
what Stage 2's R5 report actually needs today; extend when R1/R2's or
Stage 5's own requirements are known to differ, not speculatively now.
"""

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

# Caps the size of any single (chunk_resamples, len(values)) working array
# at roughly this many elements (~160MB at float64). Without this, a
# single call like `rng.choice(arr, size=(n_resamples, arr.size))` scales
# with BOTH n_resamples and len(values) at once -- fine for R5's small
# hour-of-week buckets, but a real, confirmed problem for its year buckets
# (~360,000 bars/year): a naive (2000, 360000) float64 array is ~5.8GB,
# which is not a theoretical concern -- it was observed directly, running
# this against the real 2007-2014 dataset, consuming >1GB and climbing
# before being killed. Chunking keeps peak memory bounded regardless of
# how large either dimension gets, while still using vectorised numpy
# calls (not a per-resample Python loop) within each chunk.
_MAX_ELEMENTS_PER_CHUNK = 20_000_000


@dataclass(frozen=True, slots=True)
class BootstrapCI:
    point_estimate: float
    ci_low: float
    ci_high: float
    n: int
    confidence: float
    n_resamples: int


def bootstrap_ci(
    values: np.ndarray,
    *,
    statistic: Callable[..., np.ndarray] = np.median,
    confidence: float = 0.95,
    n_resamples: int = 2000,
    seed: int,
) -> BootstrapCI:
    """Percentile bootstrap: resample `values` with replacement `n_resamples`
    times, compute `statistic` on each resample, and take the
    `(1-confidence)/2` and `1-(1-confidence)/2` percentiles of that
    resampled-statistic distribution as the CI bounds.

    `statistic` must accept an `axis` keyword argument, like `np.median`
    or `np.mean` (the default `np.median`), or `functools.partial(
    np.percentile, q=10)` for a specific quantile -- resampling is
    processed in vectorised chunks (see `_MAX_ELEMENTS_PER_CHUNK`), so the
    statistic must be able to reduce along `axis=1` in one call. This is
    deliberately not generalised to arbitrary non-vectorised callables:
    nothing in this project currently needs that, and it would be
    materially slower.

    `seed` is required, not optional -- an unseeded bootstrap makes the
    same report produce a different-looking CI on every regeneration,
    which this project's reproducibility standard ("recomputation must be
    byte-identical", Part 2 §F.1) does not tolerate for anything claiming
    to be a deterministic report. Chunking does not affect this: the same
    `np.random.default_rng(seed)` stream is drawn from in the same order
    regardless of chunk size, so results are identical across chunk sizes.

    Raises ValueError if `values` is empty -- there is no sensible CI for
    zero observations, and silently returning NaN would be a plausible-
    looking wrong result rather than a clear failure.
    """
    arr = np.asarray(values, dtype=np.float64)
    if arr.size == 0:
        raise ValueError("bootstrap_ci: values is empty")
    if not (0.0 < confidence < 1.0):
        raise ValueError(f"bootstrap_ci: confidence must be in (0, 1), got {confidence!r}")

    point_estimate = float(statistic(arr))

    rng = np.random.default_rng(seed)
    chunk_size = max(1, _MAX_ELEMENTS_PER_CHUNK // arr.size)
    resampled_stats = np.empty(n_resamples, dtype=np.float64)
    done = 0
    while done < n_resamples:
        take = min(chunk_size, n_resamples - done)
        # rng.integers + fancy indexing, not rng.choice: functionally
        # identical (uniform sampling with replacement), measurably
        # faster in practice (rng.choice carries extra bookkeeping this
        # simple case doesn't need) -- confirmed directly, not assumed,
        # profiling both against a real ~350,000-element sample.
        idx = rng.integers(0, arr.size, size=(take, arr.size))
        resampled = arr[idx]
        resampled_stats[done : done + take] = statistic(resampled, axis=1)
        done += take

    alpha = (1.0 - confidence) / 2.0
    ci_low, ci_high = np.percentile(resampled_stats, [alpha * 100.0, (1.0 - alpha) * 100.0])

    return BootstrapCI(
        point_estimate=point_estimate,
        ci_low=float(ci_low),
        ci_high=float(ci_high),
        n=arr.size,
        confidence=confidence,
        n_resamples=n_resamples,
    )
