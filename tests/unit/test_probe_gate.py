import numpy as np
import pytest

from jarvis.probe.contexts import ProbeParams
from jarvis.probe.gate import INTERSECTION_KEYS, YearCounts, evaluate_gate

_OTHER = 1000  # a large, non-narrowest value for the three intersections not under test


def _years(narrowest_values: list[int], *, start_year: int = 2007, admissible: list[bool] | None = None) -> list[YearCounts]:
    n = len(narrowest_values)
    admissible = admissible if admissible is not None else [True] * n
    years = []
    for i in range(n):
        per_context = {
            "C-A": 500,
            "C-B": 500,
            "C-C": 500,
            "C-D": 500,
            "C-A∩C-B": narrowest_values[i],
            "C-A∩C-C": _OTHER,
            "C-D∩C-B": _OTHER,
            "C-D∩C-C": _OTHER,
        }
        years.append(
            YearCounts(
                year=start_year + i,
                admissible=admissible[i],
                admissible_days=260,
                context_eligible_days=250,
                per_context=per_context,
            )
        )
    return years


def test_branch_proceed_gbpusd():
    years = _years([150] * 16)
    result = evaluate_gate(years, ProbeParams(), None)
    assert result.decision == "PROCEED_GBPUSD"
    assert result.narrowest_intersection == "C-A∩C-B"
    assert result.median_annual == pytest.approx(150.0)
    assert result.p10_annual == pytest.approx(150.0)
    assert result.percentile_method == "linear"


def test_branch_proceed_with_instability_warning():
    # 12 years: two very low (10), ten high (120). median=120 (>=100);
    # P10 via linear interpolation (rank = (12-1)*0.10 = 1.1, between the
    # sorted values 10 and 120) = 10 + 0.1*(120-10) = 21 (<60).
    values = [10, 10] + [120] * 10
    years = _years(values)
    result = evaluate_gate(years, ProbeParams(), None)
    assert result.decision == "PROCEED_GBPUSD_WITH_INSTABILITY_WARNING"
    assert result.median_annual == pytest.approx(120.0)
    assert result.p10_annual == pytest.approx(21.0)


def test_branch_widen_context_first_attempt_mid_range():
    years = _years([70] * 12)
    result = evaluate_gate(years, ProbeParams(), None)
    assert result.decision == "WIDEN_CONTEXT"
    assert result.median_annual == pytest.approx(70.0)


def test_branch_widen_context_low_range_no_prior_widening():
    years = _years([20] * 12)
    result = evaluate_gate(years, ProbeParams(), None)
    assert result.decision == "WIDEN_CONTEXT"
    assert result.median_annual == pytest.approx(20.0)
    assert result.widened_from is None


def test_branch_consider_eurusd_fallback_after_widening():
    years = _years([20] * 12)
    params = ProbeParams(range_pct_max=0.40)
    result = evaluate_gate(years, params, "range_pct_max")
    assert result.decision == "CONSIDER_EURUSD_FALLBACK"
    assert result.widened_from == "range_pct_max"


def test_branch_insufficient_data_below_12_admissible_years():
    admissible = [True] * 5 + [False] * 11
    years = _years([150] * 16, admissible=admissible)
    result = evaluate_gate(years, ProbeParams(), None)
    assert result.decision == "INSUFFICIENT_DATA"
    assert len(result.years_used) == 5
    assert np.isnan(result.median_annual)
    assert np.isnan(result.p10_annual)


def test_percentile_method_is_linear_not_nearest_or_lower():
    """A case where linear interpolation and nearest-rank/lower/higher
    genuinely differ, asserting linear is what evaluate_gate uses."""
    values = [10, 10] + [120] * 10  # same construction as the instability-warning test
    years = _years(values)
    result = evaluate_gate(years, ProbeParams(), None)

    linear = float(np.percentile(values, 10, method="linear"))
    nearest = float(np.percentile(values, 10, method="nearest"))
    lower = float(np.percentile(values, 10, method="lower"))
    higher = float(np.percentile(values, 10, method="higher"))

    assert linear != nearest
    assert linear != lower
    assert linear != higher
    assert result.p10_annual == pytest.approx(linear)
    assert result.percentile_method == "linear"


def test_narrowest_intersection_is_the_one_with_lowest_median():
    n = 16
    per_context_overrides = []
    years = []
    for i in range(n):
        per_context = {
            "C-A": 500,
            "C-B": 500,
            "C-C": 500,
            "C-D": 500,
            "C-A∩C-B": 200,
            "C-A∩C-C": 50,  # the actual narrowest
            "C-D∩C-B": 300,
            "C-D∩C-C": 400,
        }
        years.append(
            YearCounts(
                year=2007 + i,
                admissible=True,
                admissible_days=260,
                context_eligible_days=250,
                per_context=per_context,
            )
        )
    result = evaluate_gate(years, ProbeParams(), None)
    assert result.narrowest_intersection == "C-A∩C-C"
    assert result.median_annual == pytest.approx(50.0)


def test_arithmetic_field_is_populated_and_readable():
    years = _years([150] * 16)
    result = evaluate_gate(years, ProbeParams(), None)
    assert "PROCEED_GBPUSD" in result.arithmetic
    assert "linear" in result.arithmetic
    assert str(result.narrowest_intersection) in result.arithmetic


def test_intersection_keys_are_the_four_named_in_the_spec():
    assert set(INTERSECTION_KEYS) == {"C-A∩C-B", "C-A∩C-C", "C-D∩C-B", "C-D∩C-C"}
