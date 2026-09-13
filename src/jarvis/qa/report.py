"""QAReport: orchestrates the check battery (run_checks) and writes the
human-readable report + Parquet findings sidecar.

No dataset sealing here -- that is Stage 1B. run_checks only classifies
and reports; it never raises on an ERROR finding (the CLI's exit code is
the gate, per the WP's explicit "must not raise on ERROR findings" rule).

SOURCE (WP-010 / D-059): tick-level checks read from `data/tick/` (the
HistData-backed store), one month at a time, mirroring resample.py's own
retarget. The Dukascopy fetch-log-based checks (E-04/W-06 missing hours,
E-05 malformed blob, E-06 fetch log/filesystem disagreement) have no
equivalent for a monthly-import store -- a month either has a tick
Parquet file or it doesn't, and `resample_range` already raises on that
as a hole. FetchLogChecksAccumulator itself is left in jarvis.qa.checks,
dormant and untouched, per D-059's "leave working code that costs
nothing sitting unused" -- it is simply no longer called from here.
"""

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from jarvis.bars import read_bars
from jarvis.core.errors import UserError
from jarvis.core.types import Nanos
from jarvis.ingest.histdata_import import tick_path
from jarvis.sessions import load_session_set_def
from jarvis.timeengine import NS_PER_HOUR

from jarvis.qa.checks import Finding, Severity, TickChecksAccumulator, bar_level_checks


@dataclass(frozen=True, slots=True)
class QAReport:
    instrument: str
    range_start_ns: Nanos
    range_end_ns: Nanos
    findings: tuple[Finding, ...]
    errors: int
    warnings: int
    infos: int
    months_examined: int
    ticks_examined: int
    bars_examined: int
    started_utc: str
    completed_utc: str

    @property
    def sealable(self) -> bool:
        """True iff zero ERROR findings. Stage 1B will consume this; this
        package only reports it."""
        return self.errors == 0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _months_between(start_ns: Nanos, end_ns: Nanos) -> list[tuple[int, int]]:
    """Every (year, month) whose Parquet file could hold a tick in
    [start_ns, end_ns) -- end_ns is exclusive, so the last relevant
    instant is end_ns - 1. Mirrors bars/resample.py's identical helper;
    kept as a small local duplicate rather than a new shared import,
    since neither module is meant to depend on the other."""
    start_dt = datetime.fromtimestamp(start_ns // 1_000_000_000, tz=timezone.utc)
    end_dt = datetime.fromtimestamp((end_ns - 1) // 1_000_000_000, tz=timezone.utc)
    months: list[tuple[int, int]] = []
    y, m = start_dt.year, start_dt.month
    while (y, m) <= (end_dt.year, end_dt.month):
        months.append((y, m))
        m += 1
        if m == 13:
            m = 1
            y += 1
    return months


def run_checks(
    repo_root: Path,
    instrument: str,
    start_ns: Nanos,
    end_ns: Nanos,
    *,
    session_set_id: str = "fx_core",
    session_set_version: int = 1,
) -> QAReport:
    """Run every QA check over [start_ns, end_ns) and return one QAReport.

    Corrected V1 signature (Correction 1): dataset_versions do not exist
    until Stage 1B, so there is no id to resolve a range from here. When
    Stage 1B lands, a thin run_checks_for_dataset_version(dvid) wrapper
    resolves the id to a range and calls this function; that wrapper is
    out of scope for this package.

    WP-010: reads ticks from data/tick/, one month's Parquet file in
    memory at a time (mirroring resample.py's own retarget), accumulating
    only counters and small sample lists across months. A month with no
    tick Parquet file at all contributes nothing to the tick-level
    checks -- it is not an error here; resample_range is the function
    that raises on a missing month (a hole), and this function does not
    duplicate that gate.
    """
    if start_ns % NS_PER_HOUR != 0 or end_ns % NS_PER_HOUR != 0:
        raise UserError(
            f"start_ns ({start_ns}) and end_ns ({end_ns}) must both be hour-aligned"
        )
    if start_ns >= end_ns:
        raise UserError(f"start_ns ({start_ns}) must be strictly before end_ns ({end_ns})")

    started_utc = _utc_now_iso()

    session_set_def = load_session_set_def(session_set_id, session_set_version)
    thin_day_threshold = session_set_def.thin_day_threshold

    tick_acc = TickChecksAccumulator()

    months = _months_between(start_ns, end_ns)
    months_examined = len(months)
    ticks_examined = 0

    for year, month in months:
        path = tick_path(repo_root, instrument, year, month)
        if not path.is_file():
            continue

        df = pl.read_parquet(path)
        df = df.filter((pl.col("ts_utc_ns") >= start_ns) & (pl.col("ts_utc_ns") < end_ns))
        if df.height == 0:
            continue

        ticks_examined += df.height
        tick_acc.add_batch(
            df["ts_utc_ns"].to_numpy(),
            df["bid"].to_numpy(),
            df["ask"].to_numpy(),
            df["bid_volume"].fill_null(0.0).to_numpy(),
            df["ask_volume"].fill_null(0.0).to_numpy(),
            f"{year:04d}-{month:02d}",
        )

    findings: list[Finding] = list(tick_acc.finalize())

    bars_df = read_bars(repo_root, instrument, start_ns, end_ns)
    bars_examined = bars_df.height
    findings.extend(bar_level_checks(bars_df, start_ns, end_ns, thin_day_threshold))

    completed_utc = _utc_now_iso()

    errors = sum(1 for f in findings if f.severity == "ERROR")
    warnings = sum(1 for f in findings if f.severity == "WARNING")
    infos = sum(1 for f in findings if f.severity == "INFO")

    return QAReport(
        instrument=instrument,
        range_start_ns=start_ns,
        range_end_ns=end_ns,
        findings=tuple(findings),
        errors=errors,
        warnings=warnings,
        infos=infos,
        months_examined=months_examined,
        ticks_examined=ticks_examined,
        bars_examined=bars_examined,
        started_utc=started_utc,
        completed_utc=completed_utc,
    )


def _code_sha(repo_root: Path) -> str:
    """Best-effort git HEAD SHA for the report header. Falls back to
    "unknown" rather than raising: a QA run must be able to report even
    outside a git checkout (e.g. an extracted archive) -- this is metadata
    for a human reader, not a provenance guarantee (that is Stage 1B's
    job via `provenance`, which this package deliberately does not
    import)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except OSError:
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    return result.stdout.strip()


def _range_label(ns: Nanos) -> str:
    return datetime.fromtimestamp(ns // 1_000_000_000, tz=timezone.utc).strftime("%Y%m%d")


def report_path(repo_root: Path, report: QAReport, generated: datetime) -> Path:
    start_str = _range_label(report.range_start_ns)
    end_str = _range_label(report.range_end_ns)
    ts_str = generated.strftime("%Y%m%dT%H%M%SZ")
    return (
        repo_root
        / "reports"
        / "qa"
        / f"QA__{report.instrument}__{start_str}_{end_str}__{ts_str}.md"
    )


_SEVERITY_ORDER: dict[Severity, int] = {"ERROR": 0, "WARNING": 1, "INFO": 2}

# WP-013: the display cap belongs here, not in qa/checks.py's accumulation
# step -- Finding.sample now holds every matching location (the Parquet
# sidecar written below gets the full list, unmodified). This constant
# controls only how many of those the human-readable markdown shows;
# "concise markdown, complete sidecar" is the intent, not "sidecar as
# truncated as markdown."
_MARKDOWN_SAMPLE_DISPLAY_LIMIT = 10


def _render_markdown(report: QAReport, code_sha: str, generated: datetime) -> str:
    lines: list[str] = []
    lines.append(f"# QA Report -- {report.instrument}")
    lines.append("")
    lines.append(f"- Range: {_range_label(report.range_start_ns)} - {_range_label(report.range_end_ns)} (UTC, half-open)")
    lines.append(f"- ERROR: {report.errors}  WARNING: {report.warnings}  INFO: {report.infos}")
    lines.append(f"- Sealable: {report.sealable}")
    lines.append(f"- Months examined: {report.months_examined}")
    lines.append(f"- Ticks examined: {report.ticks_examined}")
    lines.append(f"- Bars examined: {report.bars_examined}")
    lines.append(f"- Code SHA: {code_sha}")
    lines.append(f"- Generated: {generated.isoformat().replace('+00:00', 'Z')}")
    lines.append(f"- Started: {report.started_utc}  Completed: {report.completed_utc}")
    lines.append("")

    ordered = sorted(
        report.findings, key=lambda f: (_SEVERITY_ORDER[f.severity], f.check_id, f.year or 0)
    )
    for severity in ("ERROR", "WARNING", "INFO"):
        section = [f for f in ordered if f.severity == severity]
        lines.append(f"## {severity} ({len(section)})")
        lines.append("")
        if not section:
            lines.append("None.")
            lines.append("")
            continue
        for finding in section:
            year_label = f" [{finding.year}]" if finding.year is not None else ""
            lines.append(f"### {finding.check_id} {finding.check_name}{year_label}")
            lines.append(f"- Count: {finding.count}")
            lines.append(f"- {finding.detail}")
            if finding.sample:
                shown = finding.sample[:_MARKDOWN_SAMPLE_DISPLAY_LIMIT]
                omitted = len(finding.sample) - len(shown)
                lines.append("- Samples:")
                for s in shown:
                    lines.append(f"  - {s}")
                if omitted > 0:
                    lines.append(
                        f"  - ... and {omitted} more (see the Parquet sidecar for the complete list)"
                    )
            lines.append("")

    return "\n".join(lines)


def write_report(repo_root: Path, report: QAReport) -> tuple[Path, Path]:
    """Write the markdown report and its Parquet findings sidecar
    (one row per finding). Returns (md_path, parquet_path)."""
    generated = datetime.now(timezone.utc)
    code_sha = _code_sha(repo_root)

    md_path = report_path(repo_root, report, generated)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(_render_markdown(report, code_sha, generated), encoding="utf-8")

    parquet_path = md_path.with_suffix(".parquet")
    findings_df = pl.DataFrame(
        {
            "check_id": [f.check_id for f in report.findings],
            "check_name": [f.check_name for f in report.findings],
            "severity": [f.severity for f in report.findings],
            "year": [f.year for f in report.findings],
            "count": [f.count for f in report.findings],
            "detail": [f.detail for f in report.findings],
            "sample": [list(f.sample) for f in report.findings],
        },
        schema={
            "check_id": pl.Utf8,
            "check_name": pl.Utf8,
            "severity": pl.Utf8,
            "year": pl.Int64,
            "count": pl.Int64,
            "detail": pl.Utf8,
            "sample": pl.List(pl.Utf8),
        },
    )
    findings_df.write_parquet(parquet_path)

    return md_path, parquet_path
