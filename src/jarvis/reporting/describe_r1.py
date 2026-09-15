"""Renders and writes R1 -- Session range anatomy (Technical Bible Part 2
§G.1.2/§G.1.3). Takes an already-computed `jarvis.describe.r1.R1Result`;
never computes anything itself -- same layering reasoning as
`describe_r5.py` (`reporting` sits above `describe`, consumes its output
only).
"""

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import polars as pl

from jarvis.describe.r1 import R1Result, RangeStats
from jarvis.reporting.boxplot import compute_box_stats, render_box_plot_svg
from jarvis.reporting.furniture import (
    closing_lines,
    code_sha,
    header_lines,
    is_suppressed,
    sample_size_note,
    utc_now_iso,
)

_WEEKDAY_NAMES = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
_SESSION_TITLES = {"pre_london": "pre_london", "london": "london", "new_york": "new_york"}


def _ns_to_date_str(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc).strftime("%Y-%m-%d")


def _fmt_ci(ci) -> str:
    if ci is None:
        return "--"
    return f"{ci.point_estimate:.6f} [{ci.ci_low:.6f}, {ci.ci_high:.6f}]"


def _fmt_iqr(q1: float | None, q3: float | None) -> str:
    if q1 is None or q3 is None:
        return "--"
    return f"[{q1:.6f}, {q3:.6f}]"


def _actual_bootstrap_params(result: R1Result) -> tuple[float, int] | None:
    """Same reasoning as describe_r5's own helper (D-068's stale-text
    lesson): read the actual confidence/n_resamples off this run's own
    BootstrapCI objects rather than hardcoding a claim in this renderer
    that could drift out of sync with describe.r1's own tuning."""
    for sess in result.sessions:
        for yc in sess.by_year:
            if yc.stats.median_price is not None:
                return yc.stats.median_price.confidence, yc.stats.median_price.n_resamples
    return None


def _stats_row_cells(stats: RangeStats) -> list[str]:
    if is_suppressed(stats.n):
        return [str(stats.n)] + ["n<10 suppressed"] * 5 + [sample_size_note(stats.n)]
    return [
        str(stats.n),
        _fmt_ci(stats.median_price),
        _fmt_iqr(stats.q1_price, stats.q3_price),
        _fmt_ci(stats.median_atr),
        _fmt_iqr(stats.q1_atr, stats.q3_atr),
        sample_size_note(stats.n),
    ]


def render_r1_markdown(result: R1Result, *, repo_root: Path, svg_filename: str) -> str:
    data_period = (
        f"{_ns_to_date_str(result.start_ns)} to {_ns_to_date_str(result.end_ns)} "
        "(UTC, half-open) -- Stage 2's fixed 2007-2014 descriptive range, per AR-1"
    )
    lines: list[str] = header_lines(
        title="R1 -- Session range anatomy",
        data_period=data_period,
        code_sha_value=code_sha(repo_root),
        generated_at=utc_now_iso(),
        session_set="fx_core v1",  # unlike R5, R1 genuinely reads session windows via jarvis.sessions
        feature_set="v1",  # and genuinely reads {session}_range / atr_bars via jarvis.features.compute
    )
    bootstrap_params = _actual_bootstrap_params(result)
    if bootstrap_params is not None:
        confidence, n_resamples = bootstrap_params
        uncertainty_note = f"{confidence * 100:.0f}% percentile bootstrap CIs (n_resamples={n_resamples})"
    else:
        uncertainty_note = "no cell had enough observations (n>=10) for a bootstrap CI"
    lines += [
        "",
        "## Question",
        "",
        "How large are the `pre_london`, `london` and `new_york` ranges, and how has that changed?",
        "",
        "## Calculation",
        "",
        "Distribution of each session's range in price and in ATR units; by year; "
        f"by day of week. Uncertainty: median with {uncertainty_note}; interquartile "
        "range; n per cell.",
        "",
        f"- Bars examined: {result.bars_examined}",
        f"- Box plot (year axis, ATR units): `{svg_filename}` -- see below",
        "",
    ]

    for sess in result.sessions:
        title = _SESSION_TITLES.get(sess.session, sess.session)
        lines += [
            f"## `{title}` -- by year",
            "",
            "| Year | n | Median range (price) [95% CI] | IQR (price) | Median range (ATR units) [95% CI] | IQR (ATR units) | Note |",
            "|---|---|---|---|---|---|---|",
        ]
        for yc in sess.by_year:
            cells = _stats_row_cells(yc.stats)
            lines.append(f"| {yc.year} | " + " | ".join(cells) + " |")

        lines += [
            "",
            f"## `{title}` -- by day of week",
            "",
            "| Day | n | Median range (price) [95% CI] | IQR (price) | Median range (ATR units) [95% CI] | IQR (ATR units) | Note |",
            "|---|---|---|---|---|---|---|",
        ]
        for wc in sess.by_weekday:
            cells = _stats_row_cells(wc.stats)
            lines.append(f"| {_WEEKDAY_NAMES.get(wc.weekday, str(wc.weekday))} | " + " | ".join(cells) + " |")
        lines.append("")

    lines += closing_lines()
    return "\n".join(lines) + "\n"


def _sidecar_frame(result: R1Result) -> pl.DataFrame:
    rows = []
    for sess in result.sessions:
        for yc in sess.by_year:
            rows.append(
                {
                    "session": sess.session,
                    "grouping": "year",
                    "key": yc.year,
                    "n": yc.stats.n,
                    "median_price": yc.stats.median_price.point_estimate if yc.stats.median_price else None,
                    "median_price_ci_low": yc.stats.median_price.ci_low if yc.stats.median_price else None,
                    "median_price_ci_high": yc.stats.median_price.ci_high if yc.stats.median_price else None,
                    "q1_price": yc.stats.q1_price,
                    "q3_price": yc.stats.q3_price,
                    "median_atr": yc.stats.median_atr.point_estimate if yc.stats.median_atr else None,
                    "median_atr_ci_low": yc.stats.median_atr.ci_low if yc.stats.median_atr else None,
                    "median_atr_ci_high": yc.stats.median_atr.ci_high if yc.stats.median_atr else None,
                    "q1_atr": yc.stats.q1_atr,
                    "q3_atr": yc.stats.q3_atr,
                }
            )
        for wc in sess.by_weekday:
            rows.append(
                {
                    "session": sess.session,
                    "grouping": "weekday",
                    "key": wc.weekday,
                    "n": wc.stats.n,
                    "median_price": wc.stats.median_price.point_estimate if wc.stats.median_price else None,
                    "median_price_ci_low": wc.stats.median_price.ci_low if wc.stats.median_price else None,
                    "median_price_ci_high": wc.stats.median_price.ci_high if wc.stats.median_price else None,
                    "q1_price": wc.stats.q1_price,
                    "q3_price": wc.stats.q3_price,
                    "median_atr": wc.stats.median_atr.point_estimate if wc.stats.median_atr else None,
                    "median_atr_ci_low": wc.stats.median_atr.ci_low if wc.stats.median_atr else None,
                    "median_atr_ci_high": wc.stats.median_atr.ci_high if wc.stats.median_atr else None,
                    "q1_atr": wc.stats.q1_atr,
                    "q3_atr": wc.stats.q3_atr,
                }
            )
    return pl.DataFrame(rows)


def _box_plot_svg(result: R1Result) -> str:
    # Gated by the SAME `is_suppressed` threshold (G.1.3: n<10) the
    # adjacent table uses for its own "n<10 suppressed" cells -- a year a
    # reader is told is unreliable in the table must never still draw a
    # box next to it. Checked directly against the real 2007-2014 dataset
    # (overnight audit, 2026-09-15): no session-year cell there has
    # 0<n<10, so this gate has never actually excluded a real box: it is
    # a defensive correctness fix, not an observed-necessary one.
    panels = []
    for sess in result.sessions:
        boxes = [
            compute_box_stats(str(yc.year), np.asarray(yc.raw_atr_values, dtype=np.float64))
            for yc in sess.by_year
            if not is_suppressed(yc.stats.n)
        ]
        panels.append((f"{sess.session} range (ATR units)", boxes))
    return render_box_plot_svg(panels)


def stage2_report_dir(repo_root: Path) -> Path:
    return repo_root / "reports" / "describe"


def write_r1_report(repo_root: Path, result: R1Result) -> tuple[Path, Path, Path]:
    """Writes `reports/describe/R1__{ts}.md`, its Parquet sidecar, and its
    box-plot SVG sidecar (D-013's markdown-plus-sidecar convention,
    extended to a chart sidecar rather than an embedded image -- WP-019's
    characterization), returning all three paths."""
    out_dir = stage2_report_dir(repo_root)
    out_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    md_path = out_dir / f"R1__{ts}.md"
    parquet_path = out_dir / f"R1__{ts}.parquet"
    svg_path = out_dir / f"R1__{ts}__boxplot.svg"

    md_text = render_r1_markdown(result, repo_root=repo_root, svg_filename=svg_path.name)
    md_path.write_text(md_text, encoding="utf-8")
    _sidecar_frame(result).write_parquet(parquet_path)
    svg_path.write_text(_box_plot_svg(result), encoding="utf-8")

    return md_path, parquet_path, svg_path
