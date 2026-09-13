"""Renders and writes R5 -- Spread and cost climate (Technical Bible Part 2
§G.1.2/§G.1.3). Takes an already-computed `jarvis.describe.r5.R5Result`;
never computes anything itself -- that would put `reporting` below
`describe` in the dependency direction the layer contract actually
allows (`reporting` is above `describe`, so it may only consume
`describe`'s output, never the reverse).
"""

from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from jarvis.describe.r5 import R5Result
from jarvis.reporting.furniture import (
    closing_lines,
    code_sha,
    header_lines,
    is_suppressed,
    sample_size_note,
    utc_now_iso,
)

_WEEKDAY_NAMES = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}


def _ns_to_date_str(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc).strftime("%Y-%m-%d")


def _fmt_ci(ci) -> str:
    if ci is None:
        return "--"
    return f"{ci.point_estimate:.6f} [{ci.ci_low:.6f}, {ci.ci_high:.6f}]"


def _actual_bootstrap_params(result: R5Result) -> tuple[float, int] | None:
    """Reads (confidence, n_resamples) from whatever this specific run's
    own `BootstrapCI` objects actually recorded, rather than a hardcoded
    string in the renderer that could silently drift out of sync with
    `describe.r5`'s own tuning (as a hardcoded "n_resamples=2000" here
    once did, discovered only by reading the real generated report
    against real data -- `describe.r5` reduces it to 500 for its
    real-scale year buckets). Returns None if every bucket was
    suppressed (n < 10), in which case no CI was computed at all."""
    for row in result.by_year:
        if row.spread_ci is not None:
            return row.spread_ci.confidence, row.spread_ci.n_resamples
    for row in result.by_hour_of_week:
        if row.spread_ci is not None:
            return row.spread_ci.confidence, row.spread_ci.n_resamples
    return None


def render_r5_markdown(result: R5Result, *, repo_root: Path) -> str:
    data_period = (
        f"{_ns_to_date_str(result.start_ns)} to {_ns_to_date_str(result.end_ns)} "
        "(UTC, half-open) -- Stage 2's fixed 2007-2014 descriptive range, per AR-1"
    )
    lines: list[str] = header_lines(
        title="R5 -- Spread and cost climate",
        data_period=data_period,
        code_sha_value=code_sha(repo_root),
        generated_at=utc_now_iso(),
    )
    bootstrap_params = _actual_bootstrap_params(result)
    if bootstrap_params is not None:
        confidence, n_resamples = bootstrap_params
        uncertainty_note = (
            f"{confidence * 100:.0f}% percentile bootstrap CIs (n_resamples={n_resamples}) "
            "on every median."
        )
    else:
        uncertainty_note = "no bucket had enough observations (n>=10) for a bootstrap CI."
    lines += [
        "",
        "## Question",
        "",
        "What does it actually cost to transact, by hour and day?",
        "",
        "## Calculation",
        "",
        "`spread_twa` distribution by hour-of-week (Europe/London local) and by "
        "year; ratio of median spread to median 60-minute range for the same "
        f"hour-of-week bucket. Uncertainty: {uncertainty_note}",
        "",
        f"- Bars examined: {result.bars_examined}",
        "",
        "## Spread and 60-minute range by hour-of-week",
        "",
        "| Weekday | Hour (London) | n | Median spread [95% CI] | Median 60m range [95% CI] | Spread/range ratio | Note |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in result.by_hour_of_week:
        if is_suppressed(row.n):
            lines.append(
                f"| {_WEEKDAY_NAMES[row.weekday]} | {row.hour:02d}:00 | {row.n} | "
                "n<10 suppressed | n<10 suppressed | n<10 suppressed | n<10 suppressed |"
            )
            continue
        ratio_str = f"{row.spread_to_range_ratio:.6f}" if row.spread_to_range_ratio is not None else "--"
        lines.append(
            f"| {_WEEKDAY_NAMES[row.weekday]} | {row.hour:02d}:00 | {row.n} | "
            f"{_fmt_ci(row.spread_ci)} | {_fmt_ci(row.range_60m_ci)} | {ratio_str} | "
            f"{sample_size_note(row.n)} |"
        )

    lines += [
        "",
        "## Spread by year",
        "",
        "| Year | n | Median spread [95% CI] | Note |",
        "|---|---|---|---|",
    ]
    for row in result.by_year:
        if is_suppressed(row.n):
            lines.append(f"| {row.year} | {row.n} | n<10 suppressed | n<10 suppressed |")
            continue
        lines.append(
            f"| {row.year} | {row.n} | {_fmt_ci(row.spread_ci)} | {sample_size_note(row.n)} |"
        )

    lines += closing_lines()
    return "\n".join(lines) + "\n"


def _sidecar_frame(result: R5Result) -> pl.DataFrame:
    rows = []
    for row in result.by_hour_of_week:
        rows.append(
            {
                "weekday": row.weekday,
                "hour": row.hour,
                "n": row.n,
                "spread_median": row.spread_ci.point_estimate if row.spread_ci else None,
                "spread_ci_low": row.spread_ci.ci_low if row.spread_ci else None,
                "spread_ci_high": row.spread_ci.ci_high if row.spread_ci else None,
                "range_60m_median": row.range_60m_ci.point_estimate if row.range_60m_ci else None,
                "range_60m_ci_low": row.range_60m_ci.ci_low if row.range_60m_ci else None,
                "range_60m_ci_high": row.range_60m_ci.ci_high if row.range_60m_ci else None,
                "spread_to_range_ratio": row.spread_to_range_ratio,
            }
        )
    return pl.DataFrame(rows)


def stage2_report_dir(repo_root: Path) -> Path:
    return repo_root / "reports" / "describe"


def write_r5_report(repo_root: Path, result: R5Result) -> tuple[Path, Path]:
    """Writes `reports/describe/R5__{ts}.md` and its Parquet sidecar
    (the by-hour-of-week table, D-013's markdown-plus-sidecar convention),
    returning both paths."""
    out_dir = stage2_report_dir(repo_root)
    out_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    md_path = out_dir / f"R5__{ts}.md"
    parquet_path = out_dir / f"R5__{ts}.parquet"

    md_path.write_text(render_r5_markdown(result, repo_root=repo_root), encoding="utf-8")
    _sidecar_frame(result).write_parquet(parquet_path)

    return md_path, parquet_path
