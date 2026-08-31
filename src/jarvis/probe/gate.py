"""The Stage 0 two-tier gate (Technical Bible Part 2 SS G.0.5). A pure
function of already-counted per-year context occurrences -- it never
touches bars, features, or ticks itself, and never computes anything
resembling profitability. The decision it produces (D-042) determines
whether the project continues on GBP/USD at all; the arithmetic behind
it is deliberately shown step by step rather than only stating the
result.

EXPLORATORY -- FREQUENCY ONLY -- NOT EVIDENCE OF PREDICTIVE VALUE.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np

from jarvis.probe.contexts import ProbeParams

GateDecision = Literal[
    "PROCEED_GBPUSD",
    "PROCEED_GBPUSD_WITH_INSTABILITY_WARNING",
    "WIDEN_CONTEXT",
    "CONSIDER_EURUSD_FALLBACK",
    "INSUFFICIENT_DATA",
]

# D-039: the four reported intersections. Direction-less contexts (C-A,
# C-D) broadcast across both C-B/C-C directions when intersected, so each
# key here is a single per-day count, not itself split by direction --
# see YearCounts.per_context's own docstring for how the broadcast is
# actually computed.
INTERSECTION_KEYS: tuple[str, ...] = ("C-A∩C-B", "C-A∩C-C", "C-D∩C-B", "C-D∩C-C")

_MIN_ADMISSIBLE_YEARS = 12
_M_PROCEED = 100
_P10_PROCEED = 60
_M_WIDEN_FLOOR = 40


@dataclass(frozen=True, slots=True)
class YearCounts:
    year: int
    admissible: bool
    admissible_days: int
    context_eligible_days: int
    per_context: Mapping[str, int]  # includes the four INTERSECTION_KEYS


@dataclass(frozen=True, slots=True)
class GateResult:
    decision: GateDecision
    narrowest_intersection: str
    median_annual: float
    p10_annual: float
    percentile_method: str
    years_used: tuple[int, ...]
    per_year: tuple[YearCounts, ...]
    params: ProbeParams
    widened_from: str | None
    arithmetic: str  # the computation, shown step by step, for the report


def evaluate_gate(
    year_counts: Sequence[YearCounts],
    params: ProbeParams,
    widened_from: str | None,
) -> GateResult:
    """D-024a: the thresholds below (M>=100, P10>=60, the 40 floor) are a
    conservative research-design choice, not a statistical law -- they
    encode an assumption that a directional filter and entry trigger
    remove 50-75% of context instances, and a judgement that ~150
    holdout events is the minimum at which a small effect is
    distinguishable from zero. They may be changed only through the
    decision log and only on evidence -- never because a probe run
    failed to clear them, and this function does not adjust them based
    on its own output.

    `widened_from` is the parameter name (if any) that was relaxed to
    produce the events THIS year_counts reflects -- i.e. it describes
    this call's own inputs, not a persisted run history. "Widening
    already attempted once" in the decision table is therefore exactly
    `widened_from is not None`: the caller (report.run_probe) is
    responsible for only ever calling this with a non-None widened_from
    after a prior baseline call already returned WIDEN_CONTEXT, and for
    never calling it a second time with a different widened_from -- this
    function has no run history to check that against itself."""
    admissible = [yc for yc in year_counts if yc.admissible]
    years_used = tuple(sorted(yc.year for yc in admissible))

    if len(admissible) < _MIN_ADMISSIBLE_YEARS:
        arithmetic = (
            f"{len(admissible)} of {len(year_counts)} candidate years admissible "
            f"(admissible years: {years_used}); at least {_MIN_ADMISSIBLE_YEARS} "
            f"are required for a gate decision -> INSUFFICIENT_DATA"
        )
        return GateResult(
            decision="INSUFFICIENT_DATA",
            narrowest_intersection="",
            median_annual=float("nan"),
            p10_annual=float("nan"),
            percentile_method="linear",
            years_used=years_used,
            per_year=tuple(year_counts),
            params=params,
            widened_from=widened_from,
            arithmetic=arithmetic,
        )

    medians: dict[str, float] = {}
    series: dict[str, list[int]] = {}
    for key in INTERSECTION_KEYS:
        values = [int(yc.per_context.get(key, 0)) for yc in admissible]
        series[key] = values
        medians[key] = float(np.median(values))

    narrowest = min(INTERSECTION_KEYS, key=lambda k: medians[k])
    narrowest_values = series[narrowest]
    median_annual = float(np.median(narrowest_values))
    p10_annual = float(np.percentile(narrowest_values, 10, method="linear"))

    if median_annual >= _M_PROCEED and p10_annual >= _P10_PROCEED:
        decision: GateDecision = "PROCEED_GBPUSD"
    elif median_annual >= _M_PROCEED:
        decision = "PROCEED_GBPUSD_WITH_INSTABILITY_WARNING"
    elif _M_WIDEN_FLOOR <= median_annual < _M_PROCEED:
        decision = "WIDEN_CONTEXT"
    else:  # median_annual < _M_WIDEN_FLOOR
        decision = "CONSIDER_EURUSD_FALLBACK" if widened_from is not None else "WIDEN_CONTEXT"

    medians_line = ", ".join(f"{k}={medians[k]:.1f}" for k in INTERSECTION_KEYS)
    arithmetic = (
        f"Admissible years: {len(admissible)} of {len(year_counts)} "
        f"({years_used[0]}-{years_used[-1]})\n"
        f"Per-intersection medians across admissible years: {medians_line}\n"
        f"Narrowest intersection (lowest median): {narrowest}\n"
        f"Per-year counts for {narrowest}: {narrowest_values}\n"
        f"M = median({narrowest_values}) = {median_annual:.2f}\n"
        f"P10 = 10th percentile ({narrowest_values}, method=linear) = {p10_annual:.2f}\n"
        f"widened_from = {widened_from!r}\n"
        f"Gate: M={median_annual:.2f} P10={p10_annual:.2f} -> {decision}"
    )

    return GateResult(
        decision=decision,
        narrowest_intersection=narrowest,
        median_annual=median_annual,
        p10_annual=p10_annual,
        percentile_method="linear",
        years_used=years_used,
        per_year=tuple(year_counts),
        params=params,
        widened_from=widened_from,
        arithmetic=arithmetic,
    )
