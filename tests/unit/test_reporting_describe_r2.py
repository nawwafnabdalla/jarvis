from datetime import datetime, timezone

import numpy as np
import polars as pl
import pytest

from jarvis.core.bootstrap import BootstrapCI
from jarvis.core.types import Nanos
from jarvis.describe.r2 import BucketStats, R2Result, YearBuckets, compute_r2
from jarvis.reporting.describe_r2 import _fmt_ci, _fmt_iqr, _fmt_pct_range, render_r2_markdown, write_r2_report
from jarvis.reporting.furniture import WATERMARK
from jarvis.sessions import load_session_set

SESSION_SET = load_session_set("fx_core", 1)
NS_PER_MINUTE = 60_000_000_000


def _weekday_bars(n_weekdays: int, *, start: datetime, seed: int = 1) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    day = start
    price = 1.3000
    added = 0
    while added < n_weekdays:
        if day.weekday() < 5:
            for m in range(24 * 60):
                ts = Nanos(int(day.timestamp()) * 1_000_000_000 + m * NS_PER_MINUTE)
                price += float(rng.normal(0, 0.00003))
                rows.append(
                    {
                        "ts_utc_ns": ts, "bid_o": price, "bid_h": price + 0.0002, "bid_l": price - 0.0002,
                        "bid_c": price, "ask_o": price + 0.0002, "ask_h": price + 0.0004, "ask_l": price,
                        "ask_c": price + 0.0002, "tick_count": 1, "first_tick_ns": ts, "last_tick_ns": ts,
                        "spread_open": 0.0002, "spread_max": 0.0002, "spread_twa": 0.0002, "prev_gap_ns": None,
                    }
                )
            added += 1
        day = datetime.fromtimestamp(day.timestamp() + 86400, tz=timezone.utc)
    return pl.DataFrame(rows)


@pytest.fixture(scope="module")
def real_result():
    bars = _weekday_bars(90, start=datetime(2010, 1, 4, tzinfo=timezone.utc))
    return compute_r2(bars, SESSION_SET, start_ns=Nanos(0), end_ns=Nanos(1))


def test_markdown_contains_watermark_first(real_result, tmp_path):
    md = render_r2_markdown(real_result, repo_root=tmp_path)
    assert md.startswith(f"# {WATERMARK}")


def test_markdown_is_ascii_only(real_result, tmp_path):
    md = render_r2_markdown(real_result, repo_root=tmp_path)
    md.encode("ascii")


def test_markdown_discloses_session_and_feature_set(real_result, tmp_path):
    md = render_r2_markdown(real_result, repo_root=tmp_path)
    assert "fx_core v1" in md
    assert "Session set: not used by this report" not in md


def test_markdown_has_pooled_and_by_year_tables_adjacent(real_result, tmp_path):
    # The literal Restriction: "the report must display the per-year table
    # adjacent to the pooled table" -- both headers present, and "By year"
    # must come immediately after "Pooled" with no other section header
    # (besides the table itself) in between.
    md = render_r2_markdown(real_result, repo_root=tmp_path)
    assert "## Pooled" in md
    assert "## By year" in md
    pooled_idx = md.index("## Pooled")
    by_year_idx = md.index("## By year")
    assert pooled_idx < by_year_idx
    between = md[pooled_idx:by_year_idx]
    assert between.count("## ") == 1  # only "## Pooled" itself -- nothing else interleaved


def test_markdown_pooled_table_has_five_bucket_rows(real_result, tmp_path):
    md = render_r2_markdown(real_result, repo_root=tmp_path)
    for label in ("Q1 (most compressed)", "Q2", "Q3", "Q4", "Q5 (least compressed)"):
        assert label in md


def test_write_r2_report_writes_two_real_files(real_result, tmp_path):
    md_path, parquet_path = write_r2_report(tmp_path, real_result)
    assert md_path.is_file()
    assert parquet_path.is_file()
    assert md_path.parent == tmp_path / "reports" / "describe"


def test_parquet_sidecar_has_pooled_and_year_scopes(real_result, tmp_path):
    _md_path, parquet_path = write_r2_report(tmp_path, real_result)
    sidecar = pl.read_parquet(parquet_path)
    assert set(sidecar["scope"].unique().to_list()) == {"pooled", "year"}
    assert set(sidecar.filter(pl.col("scope") == "pooled")["bucket"].to_list()) == {0, 1, 2, 3, 4}


def test_no_eligible_days_still_renders_without_error(tmp_path):
    from jarvis.describe.r2 import R2Result

    empty_result = R2Result(start_ns=Nanos(0), end_ns=Nanos(1), bars_examined=0, pooled=(), by_year=())
    md = render_r2_markdown(empty_result, repo_root=tmp_path)
    assert "## Pooled" in md
    assert "## By year" in md
    assert "Bars examined: 0" in md


def test_fmt_helpers_render_placeholder_for_none():
    # Coverage gap found by tonight's targeted sweep: every existing test
    # fixture happened to produce only suppressed (n<10) buckets, so
    # these None-input branches -- and the populated-bucket rendering
    # path below -- had never actually been exercised.
    assert _fmt_ci(None) == "--"
    assert _fmt_iqr(None, None) == "--"
    assert _fmt_iqr(0.1, None) == "--"
    assert _fmt_pct_range(None, None) == "--"
    assert _fmt_pct_range(0.1, None) == "--"


def test_populated_bucket_renders_real_ci_not_placeholder(tmp_path):
    # A bucket with n>=10 must render actual CI/IQR/pct-range numbers and
    # the real bootstrap-parameter disclosure line, not fall through to
    # the "no bucket had enough observations" placeholder text -- this
    # path is what every REAL 2007-2014 run actually exercises, but no
    # prior unit test constructed a fixture big enough to reach it.
    ci = BootstrapCI(point_estimate=32.5, ci_low=30.0, ci_high=35.0, n=20, confidence=0.95, n_resamples=2000)
    populated = BucketStats(bucket=0, n=20, pct_min=0.0, pct_max=0.2, median_ratio=ci, q1_ratio=25.0, q3_ratio=40.0)
    empty_buckets = tuple(
        BucketStats(bucket=b, n=0, pct_min=None, pct_max=None, median_ratio=None, q1_ratio=None, q3_ratio=None)
        for b in range(1, 5)
    )
    result = R2Result(
        start_ns=Nanos(0), end_ns=Nanos(1), bars_examined=1000,
        pooled=(populated,) + empty_buckets, by_year=(),
    )
    md = render_r2_markdown(result, repo_root=tmp_path)
    assert "32.500000 [30.000000, 35.000000]" in md
    assert "[25.000000, 40.000000]" in md
    assert "0.000-0.200" in md
    assert "95% percentile bootstrap CIs (n_resamples=2000)" in md
    assert "no bucket had enough observations" not in md


def test_bootstrap_params_falls_back_to_by_year_when_pooled_all_suppressed(tmp_path):
    # _actual_bootstrap_params's own by_year loop, distinct from its
    # pooled loop above -- exercised when every pooled bucket is
    # suppressed but a per-year bucket individually has enough data.
    empty_pooled = tuple(
        BucketStats(bucket=b, n=0, pct_min=None, pct_max=None, median_ratio=None, q1_ratio=None, q3_ratio=None)
        for b in range(5)
    )
    ci = BootstrapCI(point_estimate=30.0, ci_low=28.0, ci_high=32.0, n=12, confidence=0.95, n_resamples=2000)
    year_bucket = BucketStats(bucket=0, n=12, pct_min=0.0, pct_max=0.2, median_ratio=ci, q1_ratio=25.0, q3_ratio=35.0)
    year_buckets = (year_bucket,) + tuple(
        BucketStats(bucket=b, n=0, pct_min=None, pct_max=None, median_ratio=None, q1_ratio=None, q3_ratio=None)
        for b in range(1, 5)
    )
    result = R2Result(
        start_ns=Nanos(0), end_ns=Nanos(1), bars_examined=1000,
        pooled=empty_pooled, by_year=(YearBuckets(year=2010, buckets=year_buckets),),
    )
    md = render_r2_markdown(result, repo_root=tmp_path)
    assert "95% percentile bootstrap CIs (n_resamples=2000)" in md
