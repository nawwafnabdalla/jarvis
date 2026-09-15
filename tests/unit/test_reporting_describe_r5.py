from datetime import datetime, timezone

import numpy as np
import polars as pl
import pytest

from jarvis.core.bootstrap import BootstrapCI
from jarvis.core.types import Nanos
from jarvis.describe.r5 import HourOfWeekRow, R5Result, compute_r5
from jarvis.reporting.describe_r5 import _fmt_ci, render_r5_markdown, write_r5_report
from jarvis.reporting.furniture import WATERMARK


def _synthetic_bars(n: int, *, start_utc: datetime, seed: int = 0) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    start_ns = int(start_utc.timestamp()) * 1_000_000_000
    ts = start_ns + np.arange(n) * 60_000_000_000
    bid_c = 1.3 + np.cumsum(rng.normal(0, 0.0001, n))
    ask_c = bid_c + rng.uniform(0.00005, 0.0003, n)
    spread_twa = ask_c - bid_c
    return pl.DataFrame(
        {
            "ts_utc_ns": ts,
            "bid_o": bid_c,
            "bid_h": bid_c,
            "bid_l": bid_c,
            "bid_c": bid_c,
            "ask_o": ask_c,
            "ask_h": ask_c,
            "ask_l": ask_c,
            "ask_c": ask_c,
            "tick_count": np.ones(n, dtype=np.int32),
            "first_tick_ns": ts,
            "last_tick_ns": ts,
            "spread_open": spread_twa,
            "spread_max": spread_twa,
            "spread_twa": spread_twa,
            "prev_gap_ns": np.zeros(n, dtype=np.int64),
        }
    )


@pytest.fixture(scope="module")
def real_result():
    bars = _synthetic_bars(60 * 24 * 30, start_utc=datetime(2010, 1, 4, tzinfo=timezone.utc))
    return compute_r5(bars, start_ns=Nanos(0), end_ns=Nanos(1))


def test_markdown_contains_watermark_first(real_result, tmp_path):
    md = render_r5_markdown(real_result, repo_root=tmp_path)
    assert md.startswith(f"# {WATERMARK}")


def test_markdown_is_ascii_only(real_result, tmp_path):
    # WP-015/D-065: must never crash a cp1252 console -- confirmed at the
    # source rather than assumed, since this is a genuinely new report.
    md = render_r5_markdown(real_result, repo_root=tmp_path)
    md.encode("ascii")


def test_markdown_contains_required_sections(real_result, tmp_path):
    md = render_r5_markdown(real_result, repo_root=tmp_path)
    assert "## Question" in md
    assert "## Calculation" in md
    assert "## Spread and 60-minute range by hour-of-week" in md
    assert "## Spread by year" in md
    assert "## Interpretation" in md
    assert "pre-registered and tested independently" in md


def test_markdown_states_vault_exclusion(real_result, tmp_path):
    md = render_r5_markdown(real_result, repo_root=tmp_path)
    assert "Vault years (2023-present): excluded" in md


def test_markdown_discloses_missing_dataset_version(real_result, tmp_path):
    md = render_r5_markdown(real_result, repo_root=tmp_path)
    assert "not yet available (Stage 1B not built)" in md


def test_markdown_states_the_actual_n_resamples_used_not_a_hardcoded_value(real_result, tmp_path):
    # Regression test: the first draft of this renderer hardcoded
    # "n_resamples=2000" in this sentence while describe.r5 actually used
    # 500 for real-scale buckets -- found only by reading the real
    # generated report against real data, not by review. The claim in
    # the text must match what the result's own BootstrapCI objects
    # actually recorded.
    md = render_r5_markdown(real_result, repo_root=tmp_path)
    actual_n_resamples = next(
        row.spread_ci.n_resamples for row in real_result.by_year if row.spread_ci is not None
    )
    assert f"n_resamples={actual_n_resamples}" in md
    assert "n_resamples=2000" not in md or actual_n_resamples == 2000


def test_suppressed_row_shows_the_exact_g13_text(tmp_path):
    tiny_bars = _synthetic_bars(3, start_utc=datetime(2010, 1, 4, tzinfo=timezone.utc))
    result = compute_r5(tiny_bars, start_ns=Nanos(0), end_ns=Nanos(1))
    md = render_r5_markdown(result, repo_root=tmp_path)
    assert "n<10 suppressed" in md


def test_divergent_spread_and_range_n_render_both_distinctly(tmp_path):
    # spread n=100, range_60m_ci's own n=80 -- a real, checked-possible
    # divergence (60-bar rolling-window warmup drops its own nulls
    # separately from the spread sample), previously invisible because
    # the table showed only one `n`.
    spread_ci = BootstrapCI(point_estimate=0.0003, ci_low=0.00028, ci_high=0.00032, n=100, confidence=0.95, n_resamples=500)
    range_ci = BootstrapCI(point_estimate=0.001, ci_low=0.0009, ci_high=0.0011, n=80, confidence=0.95, n_resamples=500)
    divergent_row = HourOfWeekRow(
        weekday=1, hour=9, n=100, spread_ci=spread_ci, range_60m_ci=range_ci, spread_to_range_ratio=0.3
    )
    matching_row = HourOfWeekRow(
        weekday=1, hour=10, n=100, spread_ci=spread_ci,
        range_60m_ci=BootstrapCI(point_estimate=0.001, ci_low=0.0009, ci_high=0.0011, n=100, confidence=0.95, n_resamples=500),
        spread_to_range_ratio=0.3,
    )
    result = R5Result(
        start_ns=Nanos(0), end_ns=Nanos(1), bars_examined=1000,
        by_hour_of_week=(divergent_row, matching_row), by_year=(),
    )
    md = render_r5_markdown(result, repo_root=tmp_path)
    assert "range n=80" in md and "spread n=100" in md

    lines = [l for l in md.splitlines() if l.startswith("| Mon |")]
    assert len(lines) == 2
    divergent_line = next(l for l in lines if "09:00" in l)
    matching_line = next(l for l in lines if "10:00" in l)
    assert "range n=80" in divergent_line
    assert "range n=" not in matching_line


def test_write_r5_report_writes_real_files(real_result, tmp_path):
    md_path, parquet_path = write_r5_report(tmp_path, real_result)
    assert md_path.is_file()
    assert parquet_path.is_file()
    assert md_path.parent == tmp_path / "reports" / "describe"
    assert md_path.name.startswith("R5__")
    assert md_path.suffix == ".md"
    assert parquet_path.suffix == ".parquet"


def test_parquet_sidecar_has_one_row_per_hour_of_week_bucket(real_result, tmp_path):
    _md_path, parquet_path = write_r5_report(tmp_path, real_result)
    sidecar = pl.read_parquet(parquet_path)
    assert sidecar.height == len(real_result.by_hour_of_week)
    assert "weekday" in sidecar.columns
    assert "spread_to_range_ratio" in sidecar.columns


def test_fmt_ci_renders_placeholder_for_none():
    assert _fmt_ci(None) == "--"
