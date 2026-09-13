import numpy as np
import pytest

from jarvis.core.bootstrap import BootstrapCI, bootstrap_ci


def test_empty_values_raises():
    with pytest.raises(ValueError, match="empty"):
        bootstrap_ci(np.array([]), seed=1)


@pytest.mark.parametrize("confidence", [0.0, 1.0, -0.1, 1.1])
def test_invalid_confidence_raises(confidence):
    with pytest.raises(ValueError, match="confidence"):
        bootstrap_ci(np.array([1.0, 2.0, 3.0]), confidence=confidence, seed=1)


def test_point_estimate_is_the_real_statistic_not_a_resample_artifact():
    values = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    result = bootstrap_ci(values, seed=1)
    assert result.point_estimate == np.median(values)


def test_constant_input_has_a_degenerate_zero_width_ci():
    values = np.full(50, 7.0)
    result = bootstrap_ci(values, seed=1)
    assert result.point_estimate == 7.0
    assert result.ci_low == 7.0
    assert result.ci_high == 7.0


def test_ci_bounds_bracket_the_point_estimate_for_a_real_distribution():
    rng = np.random.default_rng(42)
    values = rng.normal(loc=10.0, scale=2.0, size=500)
    result = bootstrap_ci(values, seed=7)
    assert result.ci_low <= result.point_estimate <= result.ci_high
    assert result.ci_low < result.ci_high


def test_same_seed_is_byte_identical_across_runs():
    rng = np.random.default_rng(0)
    values = rng.exponential(size=300)
    a = bootstrap_ci(values, seed=123)
    b = bootstrap_ci(values, seed=123)
    assert a == b


def test_different_seeds_produce_different_ci_bounds():
    rng = np.random.default_rng(0)
    values = rng.exponential(size=300)
    a = bootstrap_ci(values, seed=1)
    b = bootstrap_ci(values, seed=2)
    assert (a.ci_low, a.ci_high) != (b.ci_low, b.ci_high)


def test_wider_confidence_produces_a_wider_or_equal_interval():
    rng = np.random.default_rng(0)
    values = rng.exponential(size=500)
    narrow = bootstrap_ci(values, confidence=0.50, seed=1)
    wide = bootstrap_ci(values, confidence=0.99, seed=1)
    assert (wide.ci_high - wide.ci_low) >= (narrow.ci_high - narrow.ci_low)


def test_records_n_confidence_and_n_resamples():
    values = np.arange(1.0, 21.0)
    result = bootstrap_ci(values, confidence=0.9, n_resamples=500, seed=1)
    assert result.n == 20
    assert result.confidence == 0.9
    assert result.n_resamples == 500


def test_custom_statistic_with_axis_kwarg():
    values = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
    result = bootstrap_ci(values, statistic=np.mean, seed=1)
    assert result.point_estimate == np.mean(values)


def test_result_is_a_frozen_dataclass():
    result = bootstrap_ci(np.array([1.0, 2.0, 3.0]), seed=1)
    assert isinstance(result, BootstrapCI)
    with pytest.raises(AttributeError):
        result.point_estimate = 99.0


def test_large_n_stays_memory_bounded_and_correct():
    """Regression test: a naive single-array bootstrap
    (rng.choice(arr, size=(n_resamples, arr.size))) against a
    real-scale, ~360,000-observation sample (R5's own year buckets) was
    observed directly to allocate several GB and had to be killed. This
    confirms the fix -- chunked resampling -- actually completes, quickly,
    at that scale, and still returns a sane, correctly-bracketed result."""
    rng = np.random.default_rng(0)
    values = rng.normal(loc=0.00015, scale=0.00003, size=360_000)
    result = bootstrap_ci(values, seed=99)
    assert result.n == 360_000
    assert result.ci_low <= result.point_estimate <= result.ci_high


def test_chunking_does_not_change_the_result():
    """The same seed and data must produce byte-identical results whether
    resampling happens in one chunk or many -- chunking is a memory
    optimisation, not a behaviour change."""
    import jarvis.core.bootstrap as bootstrap_module

    rng = np.random.default_rng(0)
    values = rng.exponential(size=5000)

    original = bootstrap_module._MAX_ELEMENTS_PER_CHUNK
    try:
        bootstrap_module._MAX_ELEMENTS_PER_CHUNK = 50_000_000  # one big chunk
        one_chunk = bootstrap_ci(values, seed=42, n_resamples=500)

        bootstrap_module._MAX_ELEMENTS_PER_CHUNK = 5_000  # many tiny chunks
        many_chunks = bootstrap_ci(values, seed=42, n_resamples=500)
    finally:
        bootstrap_module._MAX_ELEMENTS_PER_CHUNK = original

    assert one_chunk == many_chunks
