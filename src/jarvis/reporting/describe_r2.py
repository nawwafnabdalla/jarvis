"""Renders and writes R2 -- London range conditional on pre-London range
percentile (Technical Bible Part 2 SS G.1.2/SS G.1.3). Takes an already-
computed `jarvis.describe.r2.R2Result`; never computes anything itself --
same layering reasoning as `describe_r1.py`/`describe_r5.py` (`reporting`
sits above `describe`, consumes its output only).

Layout: the per-year table sits directly beneath the pooled table in one
flowing markdown document -- the Restriction's literal "the report must
display the per-year table adjacent to the pooled table," not a sidecar-
only table or a click-through. It is ONE table (year-major, bucket-minor
rows), not one table per year, matching the spec's own singular phrasing.
"""

from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from jarvis.describe.r2 import BucketStats, R2Result
from jarvis.reporting.furniture import (
    closing_lines,
    code_sha,
    header_lines,
    is_suppressed,
    sample_size_note,
    utc_now_iso,
)

_BUCKET_LABELS = {0: "Q1 (most compressed)", 1: "Q2", 2: "Q3", 3: "Q4", 4: "Q5 (least compressed)"}


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


def _fmt_pct_range(pct_min: float | None, pct_max: float | None) -> str:
    if pct_min is None or pct_max is None:
        return "--"
    return f"{pct_min:.3f}-{pct_max:.3f}"


def _actual_bootstrap_params(result: R2Result) -> tuple[float, int] | None:
    """Same reasoning as R1/R5's own helper (D-068's stale-text lesson):
    read the actual confidence/n_resamples off this run's own BootstrapCI
    objects rather than hardcoding a claim here that could drift out of
    sync with describe.r2's own tuning."""
    for stats in result.pooled:
        if stats.median_ratio is not None:
            return stats.median_ratio.confidence, stats.median_ratio.n_resamples
    for yb in result.by_year:
        for stats in yb.buckets:
            if stats.median_ratio is not None:
                return stats.median_ratio.confidence, stats.median_ratio.n_resamples
    return None


def _bucket_row_cells(stats: BucketStats) -> list[str]:
    if is_suppressed(stats.n):
        return [str(stats.n), _fmt_pct_range(stats.pct_min, stats.pct_max)] + ["n<10 suppressed"] * 2 + [sample_size_note(stats.n)]
    return [
        str(stats.n),
        _fmt_pct_range(stats.pct_min, stats.pct_max),
        _fmt_ci(stats.median_ratio),
        _fmt_iqr(stats.q1_ratio, stats.q3_ratio),
        sample_size_note(stats.n),
    ]


def render_r2_markdown(result: R2Result, *, repo_root: Path) -> str:
    data_period = (
        f"{_ns_to_date_str(result.start_ns)} to {_ns_to_date_str(result.end_ns)} "
        "(UTC, half-open) -- Stage 2's fixed 2007-2014 descriptive range, per AR-1"
    )
    lines: list[str] = header_lines(
        title="R2 -- London range conditional on pre-London range percentile",
        data_period=data_period,
        code_sha_value=code_sha(repo_root),
        generated_at=utc_now_iso(),
        session_set="fx_core v1",
        feature_set="v1",
    )
    bootstrap_params = _actual_bootstrap_params(result)
    if bootstrap_params is not None:
        confidence, n_resamples = bootstrap_params
        uncertainty_note = f"{confidence * 100:.0f}% percentile bootstrap CIs (n_resamples={n_resamples})"
    else:
        uncertainty_note = "no bucket had enough observations (n>=10) for a bootstrap CI"
    lines += [
        "",
        "## Question",
        "",
        "Does a compressed Asian range associate with a larger or smaller London range?",
        "",
        "## Calculation",
        "",
        "Trading days are bucketed by `pre_london_range_pct(60)` into 5 equal-count "
        "quintiles (WP-021): bucket boundaries are computed once, pooled across the "
        "whole report period, then reused unchanged for the per-year breakdown below. "
        "`pre_london_range_pct(60)` takes only 61 distinct values (k/60), so exact ties "
        "at a would-be bucket boundary are routine, not an edge case -- a run of days "
        "sharing an identical value is never split across two buckets, so realized "
        "bucket sizes deviate from an exact 1/5 split; the highest bucket in particular "
        "absorbs whatever remains after the first four are filled and can differ "
        "noticeably in size from the others. Distribution of `london_range / "
        f"atr_bars(1440)` per bucket. Uncertainty: {uncertainty_note}; interquartile "
        "range; explicit n per bucket, pooled and per year.",
        "",
        f"- Bars examined: {result.bars_examined}",
        "",
        "## Pooled",
        "",
        "| Quintile bucket | n | Pre-London pct range | Median ratio [95% CI] | IQR | Note |",
        "|---|---|---|---|---|---|",
    ]
    for stats in result.pooled:
        cells = _bucket_row_cells(stats)
        n_cell, pct_cell, *rest = cells
        lines.append(f"| {_BUCKET_LABELS[stats.bucket]} | " + " | ".join([n_cell, pct_cell] + rest) + " |")

    lines += [
        "",
        "## By year",
        "",
        "| Year | Quintile bucket | n | Pre-London pct range | Median ratio [95% CI] | IQR | Note |",
        "|---|---|---|---|---|---|---|",
    ]
    for yb in result.by_year:
        for stats in yb.buckets:
            cells = _bucket_row_cells(stats)
            n_cell, pct_cell, *rest = cells
            lines.append(
                f"| {yb.year} | {_BUCKET_LABELS[stats.bucket]} | " + " | ".join([n_cell, pct_cell] + rest) + " |"
            )
    lines.append("")

    lines += closing_lines()
    return "\n".join(lines) + "\n"


def _sidecar_frame(result: R2Result) -> pl.DataFrame:
    rows = []
    for stats in result.pooled:
        rows.append(
            {
                "scope": "pooled",
                "year": None,
                "bucket": stats.bucket,
                "n": stats.n,
                "pct_min": stats.pct_min,
                "pct_max": stats.pct_max,
                "median_ratio": stats.median_ratio.point_estimate if stats.median_ratio else None,
                "median_ratio_ci_low": stats.median_ratio.ci_low if stats.median_ratio else None,
                "median_ratio_ci_high": stats.median_ratio.ci_high if stats.median_ratio else None,
                "q1_ratio": stats.q1_ratio,
                "q3_ratio": stats.q3_ratio,
            }
        )
    for yb in result.by_year:
        for stats in yb.buckets:
            rows.append(
                {
                    "scope": "year",
                    "year": yb.year,
                    "bucket": stats.bucket,
                    "n": stats.n,
                    "pct_min": stats.pct_min,
                    "pct_max": stats.pct_max,
                    "median_ratio": stats.median_ratio.point_estimate if stats.median_ratio else None,
                    "median_ratio_ci_low": stats.median_ratio.ci_low if stats.median_ratio else None,
                    "median_ratio_ci_high": stats.median_ratio.ci_high if stats.median_ratio else None,
                    "q1_ratio": stats.q1_ratio,
                    "q3_ratio": stats.q3_ratio,
                }
            )
    return pl.DataFrame(rows)


def stage2_report_dir(repo_root: Path) -> Path:
    return repo_root / "reports" / "describe"


def write_r2_report(repo_root: Path, result: R2Result) -> tuple[Path, Path]:
    """Writes `reports/describe/R2__{ts}.md` and its Parquet sidecar
    (D-013's markdown-plus-sidecar convention), returning both paths."""
    out_dir = stage2_report_dir(repo_root)
    out_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    md_path = out_dir / f"R2__{ts}.md"
    parquet_path = out_dir / f"R2__{ts}.parquet"

    md_path.write_text(render_r2_markdown(result, repo_root=repo_root), encoding="utf-8")
    _sidecar_frame(result).write_parquet(parquet_path)

    return md_path, parquet_path
