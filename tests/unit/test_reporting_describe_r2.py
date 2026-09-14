from datetime import datetime, timezone

import numpy as np
import polars as pl
import pytest

from jarvis.core.types import Nanos
from jarvis.describe.r2 import compute_r2
from jarvis.reporting.describe_r2 import render_r2_markdown, write_r2_report
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
