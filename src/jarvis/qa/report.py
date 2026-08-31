"""QAReport: orchestrates the check battery (run_checks) and writes the
human-readable report + Parquet findings sidecar.

No dataset sealing here -- that is Stage 1B. run_checks only classifies
and reports; it never raises on an ERROR finding (the CLI's exit code is
the gate, per the WP's explicit "must not raise on ERROR findings" rule).
"""

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from jarvis.bars import read_bars
from jarvis.core.config import load_instruments
from jarvis.core.errors import IntegrityError, UserError
from jarvis.core.types import Nanos
from jarvis.ingest.fetch_log import read_fetch_log
from jarvis.ingest.parse import parse_bi5_arrays
from jarvis.ingest.urls import NS_PER_HOUR, raw_blob_path
from jarvis.sessions import load_session_set_def

from jarvis.qa.checks import (
    Finding,
    FetchLogChecksAccumulator,
    Severity,
    TickChecksAccumulator,
    bar_level_checks,
)


@dataclass(frozen=True, slots=True)
class QAReport:
    instrument: str
    range_start_ns: Nanos
    range_end_ns: Nanos
    findings: tuple[Finding, ...]
    errors: int
    warnings: int
    infos: int
    hours_examined: int
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


def _month_of(hour_utc_ns: Nanos) -> tuple[int, int]:
    dt = datetime.fromtimestamp(hour_utc_ns // 1_000_000_000, tz=timezone.utc)
    return dt.year, dt.month


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

    Re-parses every raw blob in range via parse_bi5_arrays, one hour of
    ticks in memory at a time (same discipline as WP-005's resampler),
    accumulating only counters and small sample lists across hours.
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
    point_scale = load_instruments()[instrument].point_scale

    tick_acc = TickChecksAccumulator()
    fetch_acc = FetchLogChecksAccumulator()
    month_log_cache: dict[tuple[int, int], dict] = {}

    hours_examined = 0
    ticks_examined = 0

    for raw_hour in range(start_ns, end_ns, NS_PER_HOUR):
        hour_ns = Nanos(raw_hour)
        hours_examined += 1

        path = raw_blob_path(repo_root, instrument, hour_ns)
        blob_exists = path.is_file()
        blob_size = path.stat().st_size if blob_exists else 0

        month_key = _month_of(hour_ns)
        if month_key not in month_log_cache:
            month_log_cache[month_key] = read_fetch_log(repo_root, instrument, *month_key)
        log_entry = month_log_cache[month_key].get(hour_ns)

        fetch_acc.observe_hour(hour_ns, blob_exists, blob_size, log_entry)

        if blob_exists and blob_size > 0:
            try:
                ticks = parse_bi5_arrays(path, instrument, hour_ns, point_scale)
            except IntegrityError as exc:
                fetch_acc.record_malformed(hour_ns, str(exc))
                continue
            ticks_examined += ticks.record_count
            tick_acc.add_hour(ticks, hour_ns)

    findings: list[Finding] = []
    findings.extend(tick_acc.finalize())
    findings.extend(fetch_acc.finalize())

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
        hours_examined=hours_examined,
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


def _render_markdown(report: QAReport, code_sha: str, generated: datetime) -> str:
    lines: list[str] = []
    lines.append(f"# QA Report -- {report.instrument}")
    lines.append("")
    lines.append(f"- Range: {_range_label(report.range_start_ns)} - {_range_label(report.range_end_ns)} (UTC, half-open)")
    lines.append(f"- ERROR: {report.errors}  WARNING: {report.warnings}  INFO: {report.infos}")
    lines.append(f"- Sealable: {report.sealable}")
    lines.append(f"- Hours examined: {report.hours_examined}")
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
                lines.append("- Samples:")
                for s in finding.sample:
                    lines.append(f"  - {s}")
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
