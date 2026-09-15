from datetime import datetime, timezone

import numpy as np
import polars as pl
import pytest

from jarvis.core.bootstrap import BootstrapCI
from jarvis.core.types import Nanos
from jarvis.describe.r1 import R1Result, RangeStats, SessionResult, YearCell, compute_r1
from jarvis.reporting.boxplot import compute_box_stats, render_box_plot_svg
from jarvis.reporting.describe_r1 import _box_plot_svg, _fmt_ci, _fmt_iqr, render_r1_markdown, write_r1_report
from jarvis.reporting.furniture import WATERMARK
from jarvis.sessions import load_session_set

SESSION_SET = load_session_set("fx_core", 1)
NS_PER_MINUTE = 60_000_000_000


def _weekday_bars(n_weekdays: int, *, start: datetime) -> pl.DataFrame:
    import numpy as np

    rng = np.random.default_rng(1)
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
    bars = _weekday_bars(40, start=datetime(2010, 1, 4, tzinfo=timezone.utc))
    return compute_r1(bars, SESSION_SET, start_ns=Nanos(0), end_ns=Nanos(1))


def test_markdown_contains_watermark_first(real_result, tmp_path):
    md = render_r1_markdown(real_result, repo_root=tmp_path, svg_filename="x.svg")
    assert md.startswith(f"# {WATERMARK}")


def test_markdown_is_ascii_only(real_result, tmp_path):
    md = render_r1_markdown(real_result, repo_root=tmp_path, svg_filename="x.svg")
    md.encode("ascii")


def test_markdown_contains_all_three_sessions(real_result, tmp_path):
    md = render_r1_markdown(real_result, repo_root=tmp_path, svg_filename="x.svg")
    for session in ("pre_london", "london", "new_york"):
        assert f"`{session}` -- by year" in md
        assert f"`{session}` -- by day of week" in md


def test_markdown_references_the_svg_by_name(real_result, tmp_path):
    md = render_r1_markdown(real_result, repo_root=tmp_path, svg_filename="R1__20260101T000000Z__boxplot.svg")
    assert "R1__20260101T000000Z__boxplot.svg" in md


def test_markdown_discloses_session_set_is_used(real_result, tmp_path):
    # Unlike R5, R1 genuinely uses a session set -- the header must say so,
    # not repeat R5's "not used by this report" boilerplate verbatim.
    md = render_r1_markdown(real_result, repo_root=tmp_path, svg_filename="x.svg")
    assert "fx_core v1" in md
    assert "Session set: not used by this report" not in md


def test_write_r1_report_writes_three_real_files(real_result, tmp_path):
    md_path, parquet_path, svg_path = write_r1_report(tmp_path, real_result)
    assert md_path.is_file()
    assert parquet_path.is_file()
    assert svg_path.is_file()
    assert md_path.parent == tmp_path / "reports" / "describe"
    assert svg_path.name.endswith("__boxplot.svg")


def test_write_r1_report_markdown_references_the_actual_svg_written(real_result, tmp_path):
    md_path, _parquet_path, svg_path = write_r1_report(tmp_path, real_result)
    md_text = md_path.read_text(encoding="utf-8")
    assert svg_path.name in md_text


def test_parquet_sidecar_has_year_and_weekday_rows(real_result, tmp_path):
    _md_path, parquet_path, _svg_path = write_r1_report(tmp_path, real_result)
    sidecar = pl.read_parquet(parquet_path)
    assert set(sidecar["grouping"].unique().to_list()) == {"year", "weekday"}
    assert set(sidecar["session"].unique().to_list()) == {"pre_london", "london", "new_york"}


def test_svg_sidecar_has_three_panels(real_result, tmp_path):
    _md_path, _parquet_path, svg_path = write_r1_report(tmp_path, real_result)
    svg_text = svg_path.read_text(encoding="utf-8")
    for session in ("pre_london", "london", "new_york"):
        assert session in svg_text


def _stats(n: int) -> RangeStats:
    if n == 0:
        return RangeStats(n=0, median_price=None, median_atr=None, q1_price=None, q3_price=None, q1_atr=None, q3_atr=None)
    ci = BootstrapCI(point_estimate=1.0, ci_low=0.9, ci_high=1.1, n=n, confidence=0.95, n_resamples=100)
    return RangeStats(n=n, median_price=ci, median_atr=ci, q1_price=0.5, q3_price=1.5, q1_atr=0.5, q3_atr=1.5)


def test_box_plot_excludes_a_year_the_table_would_suppress():
    # A year with n=5 (below G.1.3's n<10 suppression floor -- the table
    # would render it "n<10 suppressed") must not still draw a box next
    # to a table that calls it unreliable. A year with n=15 (above the
    # floor) must still appear normally.
    suppressed_year = YearCell(year=2007, stats=_stats(5), raw_atr_values=tuple(float(i) for i in range(5)))
    normal_year = YearCell(year=2008, stats=_stats(15), raw_atr_values=tuple(float(i) for i in range(15)))
    result = R1Result(
        start_ns=Nanos(0),
        end_ns=Nanos(1),
        bars_examined=100,
        sessions=(
            SessionResult(session="pre_london", by_year=(suppressed_year, normal_year), by_weekday=()),
        ),
    )
    svg = _box_plot_svg(result)
    assert "2008" in svg
    assert "2007" not in svg


def test_fmt_ci_renders_placeholder_for_none():
    assert _fmt_ci(None) == "--"


def test_fmt_iqr_renders_placeholder_for_none():
    assert _fmt_iqr(None, None) == "--"
    assert _fmt_iqr(0.1, None) == "--"


def test_box_plot_handles_perfectly_flat_data_without_dividing_by_zero():
    # boxplot.py's own y_min==y_max guard (protects the later
    # (v-y_min)/(y_max-y_min) scaling division from a zero denominator)
    # -- already present in the code, but no existing test (all of which
    # use real or randomly-noised fixtures) had ever actually exercised
    # perfectly identical values across a whole panel. Confirmed the
    # guard genuinely works, not just that it reads plausibly.
    flat_box = compute_box_stats("2020", np.array([5.0, 5.0, 5.0, 5.0, 5.0]))
    svg = render_box_plot_svg([("flat panel", [flat_box])])
    assert "<svg" in svg and svg.strip().endswith("</svg>")
    assert "nan" not in svg.lower() and "inf" not in svg.lower()
