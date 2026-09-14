import numpy as np
import pytest

from jarvis.reporting.boxplot import BoxStats, compute_box_stats, render_box_plot_svg


def test_compute_box_stats_hand_computed():
    values = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0])
    stats = compute_box_stats("test", values)
    assert stats.q1 == pytest.approx(3.0)
    assert stats.median == pytest.approx(5.0)
    assert stats.q3 == pytest.approx(7.0)
    assert stats.n == 9
    assert stats.outliers == ()


def test_compute_box_stats_detects_outlier():
    values = np.array([1.0, 2.0, 2.0, 3.0, 3.0, 3.0, 4.0, 4.0, 5.0, 100.0])
    stats = compute_box_stats("test", values)
    assert 100.0 in stats.outliers
    assert stats.whisker_hi < 100.0


def test_compute_box_stats_no_outliers_whiskers_bracket_data():
    rng = np.random.default_rng(0)
    values = rng.normal(0, 1, 200)
    stats = compute_box_stats("test", values)
    assert stats.whisker_lo <= stats.q1 <= stats.median <= stats.q3 <= stats.whisker_hi


def test_render_box_plot_svg_structural_shape():
    boxes_a = [compute_box_stats(str(y), np.array([1.0, 2.0, 3.0, 4.0, 5.0])) for y in range(2010, 2013)]
    boxes_b = [compute_box_stats(str(y), np.array([10.0, 20.0, 30.0])) for y in range(2010, 2014)]
    svg = render_box_plot_svg([("panel one", boxes_a), ("panel two", boxes_b)])

    assert svg.startswith("<svg")
    assert svg.endswith("</svg>")
    # 3 + 4 = 7 boxes total across both panels.
    assert svg.count('<rect class="box"') == 7
    assert "panel one" in svg
    assert "panel two" in svg


def test_render_box_plot_svg_is_ascii_only():
    # WP-015/D-065 discipline extended to a new output type: this SVG may
    # eventually be opened or its path echoed on a cp1252 console the same
    # way any other report artifact is.
    boxes = [compute_box_stats(str(y), np.array([1.0, 2.0, 3.0])) for y in range(2010, 2012)]
    svg = render_box_plot_svg([("session", boxes)])
    svg.encode("ascii")


def test_render_box_plot_svg_empty_panel_does_not_crash():
    svg = render_box_plot_svg([("empty", [])])
    assert svg.startswith("<svg")
    assert svg.count('<rect class="box"') == 0


def test_render_box_plot_svg_handles_outliers():
    values = np.array([1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 50.0])
    boxes = [compute_box_stats("2010", values)]
    svg = render_box_plot_svg([("session", boxes)])
    assert svg.count('<circle class="outlier"') == 1
