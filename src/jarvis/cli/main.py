"""jarvis CLI entry point."""

import importlib.metadata
import platform
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import typer

from jarvis.bars import read_bars, resample_range
from jarvis.core.config import load_instruments, load_periods, repo_root
from jarvis.core.errors import ConfigError, JarvisError, OutputError, UserError
from jarvis.core.hashing import sha256_file
from jarvis.core.types import Nanos
from jarvis.describe.periods import stage2_descriptive_range
from jarvis.describe.r5 import compute_r5
from jarvis.features import REGISTRY, compute, write_features
from jarvis.ingest.fetch import ingest_range
from jarvis.ingest.histdata_import import import_histdata
from jarvis.ingest.urls import NS_PER_HOUR
from jarvis.probe.report import (
    VAULT_BOUNDARY_NS,
    has_prior_widening,
    read_lineage,
    record_lineage_run,
    reject_vault_range,
    run_probe,
)
from jarvis.probe.report import write_report as write_stage0_report
from jarvis.qa.report import run_checks, write_report
from jarvis.reporting.describe_r5 import write_r5_report
from jarvis.sessions import load_session_set

# Windows consoles commonly use a single-byte encoding (e.g. cp1252) that
# cannot represent every codepoint a report may contain (e.g. U+2229 '∩' in
# stage0's narrowest_intersection). Without this, printing such a value
# raises an unhandled UnicodeEncodeError that bypasses the JarvisError
# handling below entirely (WP-015/D-065). backslashreplace keeps the
# terminal summary readable instead of crashing; the full report is always
# also written to disk in its native UTF-8 encoding, unaffected by this.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="backslashreplace")

app = typer.Typer(name="jarvis")

_EXPECTED_DIRS = ("config", "src", "tests", "data", "ledger")
_DIRS_EXPECTED_MISSING = {"data": "WP-001", "ledger": "WP-00x"}


def _echo_summary(lines: list[str]) -> None:
    """Print a command's terminal summary. The underlying computation and
    report-writing (if any) have already completed by the time this runs --
    a failure here (e.g. an encoding error the reconfigure above didn't
    catch) means only the summary itself failed to print, never that the
    computation did. Raised as OutputError so callers get a documented exit
    code (4) instead of an unhandled crash, regardless of what specifically
    caused the failure (WP-015)."""
    try:
        for line in lines:
            typer.echo(line)
    except Exception as exc:
        raise OutputError(f"failed to print command output: {exc}") from exc


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """jarvis: local-only GBPUSD research and backtesting toolkit.

    A Typer app with a single registered command collapses that command onto
    the bare invocation unless a group-level callback is present; this
    callback keeps `doctor` addressable as `jarvis doctor`. Help-and-exit-0
    on no arguments is handled explicitly here rather than via Typer's
    `no_args_is_help`, whose exit code changed to 2 (a UsageError) in
    Click 8.5.
    """
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(code=0)


@app.command()
def doctor() -> None:
    """Print: python version, platform, repo root, tzdata version,
    dependency lock hash, free disk space on the repo's drive,
    presence/absence of each expected directory, and whether config
    files load. Exit 0 if all present, 1 if any config fails to load."""
    try:
        root = repo_root()

        py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        py_impl = platform.python_implementation()
        plat = platform.platform()

        try:
            tzdata_version = importlib.metadata.version("tzdata")
        except importlib.metadata.PackageNotFoundError:
            tzdata_version = "NOT INSTALLED"

        lock_path = root / "requirements.lock"
        if lock_path.is_file():
            lock_hash = sha256_file(lock_path)
            lock_name = lock_path.name
        else:
            lock_hash = "MISSING"
            lock_name = "requirements.lock"

        usage = shutil.disk_usage(root.anchor)
        free_gb = usage.free / (1024**3)

        dir_lines = []
        for name in _EXPECTED_DIRS:
            exists = (root / name).is_dir()
            status = "OK" if exists else "MISSING"
            if not exists and name in _DIRS_EXPECTED_MISSING:
                status += f" (expected until {_DIRS_EXPECTED_MISSING[name]})"
            dir_lines.append(f"{name} {status}")

        config_ok = True
        config_lines = []
        try:
            load_instruments()
            config_lines.append("instruments.yaml OK (GBPUSD)")
        except ConfigError as exc:
            config_ok = False
            config_lines.append(f"instruments.yaml FAILED ({exc})")

        try:
            periods = load_periods()
            dev = periods["development"]
            val = periods["validation"]
            hold = periods["holdout"]
            hold_end = hold[1] if hold[1] is not None else "present"
            config_lines.append(
                "periods.yaml OK "
                f"(development {dev[0]}..{dev[1]}, "
                f"validation {val[0]}..{val[1]}, "
                f"holdout {hold[0]}..{hold_end})"
            )
        except ConfigError as exc:
            config_ok = False
            config_lines.append(f"periods.yaml FAILED ({exc})")

        lines = [
            "jarvis doctor",
            f"  Python        {py_version}  ({py_impl}, {sys.platform})",
            f"  Platform      {plat}",
            f"  Repo root     {root}",
            f"  tzdata        {tzdata_version}",
            f"  Deps lock     {lock_hash}  ({lock_name})",
            f"  Free disk     {free_gb:.1f} GB on {root.drive or root.anchor}",
            f"  Directories   {'  '.join(dir_lines)}",
            f"  Config        {config_lines[0]}",
        ]
        for line in config_lines[1:]:
            lines.append(f"                {line}")
        lines.append(f"  Status        {'OK' if config_ok else 'FAILED'}")
        _echo_summary(lines)
    except JarvisError as exc:
        typer.echo(f"jarvis doctor: {exc}")
        raise typer.Exit(code=exc.exit_code) from exc

    if not config_ok:
        raise typer.Exit(code=1)


data_app = typer.Typer(name="data", help="Market data ingestion and validation.")
app.add_typer(data_app, name="data")

features_app = typer.Typer(name="features", help="Feature computation.")
app.add_typer(features_app, name="features")

stage0_app = typer.Typer(name="stage0", help="Stage 0 feasibility probe.")
app.add_typer(stage0_app, name="stage0")

describe_app = typer.Typer(name="describe", help="Stage 2 market description reports.")
app.add_typer(describe_app, name="describe")

_INSTRUMENT = "GBPUSD"
_IMPLEMENTED_REPORTS = ("R5",)


def _parse_iso_utc_ns(value: str, *, option_name: str) -> Nanos:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise UserError(f"{option_name}: not a valid ISO 8601 timestamp: {value!r}") from exc
    if dt.tzinfo is None:
        raise UserError(f"{option_name}: timestamp must be timezone-aware UTC: {value!r}")
    dt = dt.astimezone(timezone.utc)
    ns = int(dt.timestamp()) * 1_000_000_000
    if ns % NS_PER_HOUR != 0:
        raise UserError(f"{option_name}: timestamp must be hour-aligned UTC: {value!r}")
    return Nanos(ns)


def _format_elapsed(seconds: float) -> str:
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


@data_app.command("fetch")
def data_fetch(
    from_: str = typer.Option(..., "--from", help="ISO 8601 UTC, hour-aligned"),
    to: str = typer.Option(..., "--to", help="ISO 8601 UTC, hour-aligned, exclusive"),
    force_refetch: bool = typer.Option(False, "--force-refetch"),
    concurrency: int = typer.Option(4, "--concurrency"),
    min_seconds_between_requests: float = typer.Option(
        0.25,
        "--min-seconds-between-requests",
        help="Global minimum spacing between requests, across all workers combined.",
    ),
    max_attempts: int = typer.Option(3, "--max-attempts"),
    timeout_seconds: float = typer.Option(30.0, "--timeout-seconds"),
) -> None:
    """Fetch raw GBP/USD tick data for the given UTC range."""
    try:
        start_ns = _parse_iso_utc_ns(from_, option_name="--from")
        end_ns = _parse_iso_utc_ns(to, option_name="--to")
        root = repo_root()

        hours_expected = (end_ns - start_ns) // NS_PER_HOUR
        typer.echo(
            f"Fetching {_INSTRUMENT}  {from_} -> {to}  "
            f"({hours_expected} hours, concurrency={concurrency})"
        )

        started = time.perf_counter()
        report = ingest_range(
            root,
            _INSTRUMENT,
            start_ns,
            end_ns,
            force_refetch=force_refetch,
            concurrency=concurrency,
            min_seconds_between_requests=min_seconds_between_requests,
            max_attempts=max_attempts,
            timeout_seconds=timeout_seconds,
        )
        elapsed = time.perf_counter() - started

        _echo_summary(
            [
                "",
                f"  Fetched            {report.hours_fetched}",
                f"  Empty (no data)    {report.hours_empty}",
                f"  Skipped (existing) {report.hours_skipped_existing}",
                f"  Missing            {report.hours_missing}",
                f"  Rate limited       {report.hours_rate_limited}",
                f"  Total bytes        {report.total_bytes:,}",
                f"  Elapsed            {_format_elapsed(elapsed)}",
            ]
        )
    except JarvisError as exc:
        typer.echo(f"jarvis data fetch: {exc}")
        raise typer.Exit(code=exc.exit_code) from exc


@data_app.command("resample")
def data_resample(
    from_: str = typer.Option(..., "--from", help="ISO 8601 UTC, hour-aligned"),
    to: str = typer.Option(..., "--to", help="ISO 8601 UTC, hour-aligned, exclusive"),
    allow_incomplete: bool = typer.Option(
        False,
        "--allow-incomplete",
        help="Proceed even if some months in range have no tick data on disk (a hole).",
    ),
) -> None:
    """Resample GBP/USD ticks from data/tick/ for the given UTC range into
    1-minute bars (WP-010: retargeted from the Dukascopy raw-blob source
    to the HistData-backed tick store; see D-059)."""
    try:
        start_ns = _parse_iso_utc_ns(from_, option_name="--from")
        end_ns = _parse_iso_utc_ns(to, option_name="--to")
        reject_vault_range(end_ns, caller="data resample")
        root = repo_root()

        hours_expected = (end_ns - start_ns) // NS_PER_HOUR
        typer.echo(
            f"Resampling {_INSTRUMENT}  {from_} -> {to}  ({hours_expected} hours)"
        )

        started = time.perf_counter()
        report = resample_range(
            root,
            _INSTRUMENT,
            start_ns,
            end_ns,
            allow_incomplete=allow_incomplete,
        )
        elapsed = time.perf_counter() - started

        _echo_summary(
            [
                "",
                f"  Months with data   {report.months_with_data}",
                f"  Months missing     {report.months_missing}",
                f"  Ticks read         {report.ticks_read}",
                f"  Bars written       {report.bars_written}",
                f"  Minutes absent     {report.minutes_absent}",
                f"  Months written     {', '.join(report.months_written)}",
                f"  Elapsed            {_format_elapsed(elapsed)}",
            ]
        )
    except JarvisError as exc:
        typer.echo(f"jarvis data resample: {exc}")
        raise typer.Exit(code=exc.exit_code) from exc


@data_app.command("validate")
def data_validate(
    from_: str = typer.Option(..., "--from", help="ISO 8601 UTC, hour-aligned"),
    to: str = typer.Option(..., "--to", help="ISO 8601 UTC, hour-aligned, exclusive"),
) -> None:
    """Run the QA check suite over the given UTC range and write a report.

    Exit code 3 if any ERROR finding exists, 0 otherwise -- usable as a
    gate in a script. WARNING and INFO findings never affect the exit
    code."""
    try:
        start_ns = _parse_iso_utc_ns(from_, option_name="--from")
        end_ns = _parse_iso_utc_ns(to, option_name="--to")
        reject_vault_range(end_ns, caller="data validate")
        root = repo_root()

        hours_expected = (end_ns - start_ns) // NS_PER_HOUR
        typer.echo(
            f"Validating {_INSTRUMENT}  {from_} -> {to}  ({hours_expected} hours)"
        )

        started = time.perf_counter()
        report = run_checks(root, _INSTRUMENT, start_ns, end_ns)
        elapsed = time.perf_counter() - started
        md_path, _parquet_path = write_report(root, report)

        _echo_summary(
            [
                "",
                f"  ERROR              {report.errors}",
                f"  WARNING            {report.warnings}",
                f"  INFO               {report.infos}",
                f"  Sealable           {report.sealable}",
                f"  Report             {md_path}",
                f"  Elapsed            {_format_elapsed(elapsed)}",
            ]
        )
    except JarvisError as exc:
        typer.echo(f"jarvis data validate: {exc}")
        raise typer.Exit(code=exc.exit_code) from exc

    if report.errors > 0:
        raise typer.Exit(code=3)


def _parse_yearmonth(value: str, *, option_name: str) -> tuple[int, int]:
    try:
        year_s, month_s = value.split("-")
        year, month = int(year_s), int(month_s)
    except ValueError as exc:
        raise UserError(f"{option_name}: not a valid YYYY-MM value: {value!r}") from exc
    if not (1 <= month <= 12):
        raise UserError(f"{option_name}: month out of range in {value!r}")
    return year, month


@data_app.command("import-histdata")
def data_import_histdata(
    source: str = typer.Option(
        ..., "--source", help="A zip of monthly zips, a directory of monthly zips, or a directory of CSVs"
    ),
    from_: str = typer.Option(None, "--from", help="YYYY-MM, inclusive"),
    to: str = typer.Option(None, "--to", help="YYYY-MM, inclusive"),
    force: bool = typer.Option(False, "--force", help="Re-import months even if already present"),
) -> None:
    """Import HistData.com monthly tick CSVs into the tick Parquet layer.

    Per-file stamp clock (us_dst vs eu_dst, D-055) and column order
    (bid_ask vs ask_bid vs mixed_per_day, D-055h/D-056) are each detected
    independently for every month, from that file's own content only --
    never carried from a neighbour. A mix of us_dst and eu_dst months in
    one import is the EXPECTED, correct outcome for any range spanning
    2018/2019 (D-055: HistData's stamps changed which DST calendar they
    follow around that boundary) -- not a fault to warn about. Prints a
    per-month breakdown plus a summary tally of each."""
    try:
        source_path = Path(source)
        start = _parse_yearmonth(from_, option_name="--from") if from_ else None
        end = _parse_yearmonth(to, option_name="--to") if to else None
        root = repo_root()

        typer.echo(f"Importing HistData  {_INSTRUMENT}  source={source_path}  from={from_}  to={to}")

        started = time.perf_counter()
        report = import_histdata(root, source_path, _INSTRUMENT, start=start, end=end, force=force)
        elapsed = time.perf_counter() - started

        lines = [""]
        for key in sorted(report.stamp_clocks):
            lines.append(
                f"  {key}  stamp_clock={report.stamp_clocks[key]:<14} "
                f"declared_gaps={report.gap_reports.get(key, 0)}"
            )
        lines.append("")
        lines.append(f"  Months found       {report.months_found}")
        lines.append(f"  Months imported    {report.months_imported}")
        lines.append(f"  Months skipped     {len(report.months_skipped)}")
        lines.append(f"  Total ticks        {report.total_ticks:,}")

        clock_tally: dict[str, int] = {}
        for clock in report.stamp_clocks.values():
            clock_tally[clock] = clock_tally.get(clock, 0) + 1
        lines.append(f"  Stamp clock tally  {dict(sorted(clock_tally.items()))}")

        lines.append(f"  Elapsed            {_format_elapsed(elapsed)}")
        if report.months_imported > 0:
            per_month = elapsed / report.months_imported
            lines.append(f"  Throughput         {per_month:.2f}s/month")
        _echo_summary(lines)
    except JarvisError as exc:
        typer.echo(f"jarvis data import-histdata: {exc}")
        raise typer.Exit(code=exc.exit_code) from exc


@features_app.command("build")
def features_build(
    from_: str = typer.Option(..., "--from", help="ISO 8601 UTC, hour-aligned"),
    to: str = typer.Option(..., "--to", help="ISO 8601 UTC, hour-aligned, exclusive"),
    features: str = typer.Option(
        None,
        "--features",
        help="Comma-separated feature names to compute; default is every registered feature.",
    ),
) -> None:
    """Compute features over bars in the given UTC range (loaded via
    bars.read_bars in this CLI layer -- jarvis.features itself never
    opens Parquet) and write them, month by month, with the same merge
    semantics as bars.store.write_bars (D-045)."""
    try:
        start_ns = _parse_iso_utc_ns(from_, option_name="--from")
        end_ns = _parse_iso_utc_ns(to, option_name="--to")
        reject_vault_range(end_ns, caller="features build")
        root = repo_root()
        names = tuple(n.strip() for n in features.split(",")) if features else tuple(REGISTRY)

        bars_df = read_bars(root, _INSTRUMENT, start_ns, end_ns)
        if bars_df.height == 0:
            _echo_summary(["No bars in range; nothing to compute."])
            raise typer.Exit(code=0)

        session_set = load_session_set("fx_core", 1)

        started = time.perf_counter()
        result = compute(names, bars_df, session_set)

        dated = result.frame.with_columns(
            pl.from_epoch(pl.col("ts_utc_ns"), time_unit="ns").alias("_dt")
        ).with_columns(
            [
                pl.col("_dt").dt.year().alias("_year"),
                pl.col("_dt").dt.month().alias("_month"),
            ]
        )
        months_written: list[str] = []
        for (year, month), month_frame in dated.group_by(["_year", "_month"], maintain_order=True):
            write_features(root, _INSTRUMENT, year, month, month_frame.drop(["_dt", "_year", "_month"]))
            months_written.append(f"{year:04d}-{month:02d}")
        elapsed = time.perf_counter() - started

        lines = [
            "",
            f"  Bars               {bars_df.height}",
            f"  Feature set        v{result.feature_set_version}",
            f"  Months written     {', '.join(months_written)}",
            "  Null counts:",
        ]
        for name in result.feature_names:
            lines.append(f"    {name:<24} {result.null_counts[name]}")
        lines.append(f"  Elapsed            {_format_elapsed(elapsed)}")
        _echo_summary(lines)
    except JarvisError as exc:
        typer.echo(f"jarvis features build: {exc}")
        raise typer.Exit(code=exc.exit_code) from exc


@stage0_app.command("probe")
def stage0_probe(
    from_: str = typer.Option(..., "--from", help="ISO 8601 UTC, hour-aligned"),
    to: str = typer.Option(..., "--to", help="ISO 8601 UTC, hour-aligned, exclusive"),
    widen: str = typer.Option(
        None,
        "--widen",
        help="Relax exactly one parameter and re-run: range_pct_max or break_buffer_atr.",
    ),
) -> None:
    """Run the Stage 0 feasibility probe (EXPLORATORY -- FREQUENCY ONLY --
    NOT EVIDENCE OF PREDICTIVE VALUE) and write a report. Refuses any
    range touching 2023-01-01 or later -- the vault is untouched, even
    for Stage 0's own descriptive purposes (PDLA-03/D-021). Exit 0 on
    PROCEED_*, exit 3 on WIDEN_CONTEXT / CONSIDER_EURUSD_FALLBACK /
    INSUFFICIENT_DATA, exit 4 if the gate computed and the report was
    written but printing the terminal summary itself failed (check the
    report file directly in that case; see OutputError, D-065)."""
    try:
        start_ns = _parse_iso_utc_ns(from_, option_name="--from")
        end_ns = _parse_iso_utc_ns(to, option_name="--to")
        # Strict >: end_ns is an EXCLUSIVE upper bound (this project's
        # [start, end) convention since WP-001), so --to
        # 2023-01-01T00:00:00Z reads through 2022-12-31T23:59:59.999999999Z
        # and touches no vault data -- it must be ALLOWED, not refused.
        if end_ns > VAULT_BOUNDARY_NS:
            raise UserError(
                f"--to ({to}) exceeds the vault boundary (2023-01-01T00:00:00Z) "
                "-- --to must not exceed 2023-01-01T00:00:00Z (exclusive). "
                "Stage 0 runs on 2007-2022 only (PDLA-03/D-021); the vault is "
                "untouched, including for descriptive purposes"
            )
        root = repo_root()

        prior_runs = read_lineage(root, _INSTRUMENT, start_ns, end_ns)
        if widen is not None and has_prior_widening(prior_runs):
            raise UserError(
                f"a widening has already been attempted for this run lineage "
                f"({len(prior_runs)} prior run(s) recorded for "
                f"{_INSTRUMENT} {from_}->{to}) -- only one widening is permitted; "
                "repeated widening until the gate passes is parameter mining"
            )

        typer.echo(f"Stage 0 probe  {_INSTRUMENT}  {from_} -> {to}  (widen={widen})")

        started = time.perf_counter()
        gate_result, events = run_probe(root, _INSTRUMENT, start_ns, end_ns, widen=widen)
        elapsed = time.perf_counter() - started

        record_lineage_run(
            root, _INSTRUMENT, start_ns, end_ns, widened_from=widen, decision=gate_result.decision
        )

        session_set = load_session_set("fx_core", 1)
        md_path, _parquet_path = write_stage0_report(
            root,
            _INSTRUMENT,
            start_ns,
            end_ns,
            session_set,
            gate_result,
            events,
            prior_run_count=len(prior_runs),
        )

        _echo_summary(
            [
                "",
                f"  Decision           {gate_result.decision}",
                f"  Median annual (M)  {gate_result.median_annual}",
                f"  P10 annual         {gate_result.p10_annual}",
                f"  Narrowest          {gate_result.narrowest_intersection}",
                f"  Report             {md_path}",
                f"  Elapsed            {_format_elapsed(elapsed)}",
            ]
        )
    except JarvisError as exc:
        typer.echo(f"jarvis stage0 probe: {exc}")
        raise typer.Exit(code=exc.exit_code) from exc

    if gate_result.decision not in ("PROCEED_GBPUSD", "PROCEED_GBPUSD_WITH_INSTABILITY_WARNING"):
        raise typer.Exit(code=3)


@describe_app.command("run")
def describe_run(
    report: str = typer.Option(..., "--report", help="R1|R2|R5 (only R5 is implemented so far)"),
    year: int = typer.Option(
        None,
        "--year",
        help="Not yet supported -- R5 always covers the full fixed 2007-2014 range.",
    ),
) -> None:
    """Run a Stage 2 market description report (Technical Bible Part 2
    SS G.1). Every report in this suite computes over a fixed, non-
    negotiable range -- 2007-2014 for R1/R2/R5, per AR-1 (D-067) -- so
    unlike the Stage 1A pipeline commands, there is no --from/--to: the
    range is not a parameter a caller can choose or forget.

    --year is part of this command's documented signature
    (Technical Bible Part 4 SS Q) but its intended behaviour for R5 is not
    specified anywhere in the Bible -- refused with a clear error rather
    than guessed at, per WP-019."""
    try:
        if year is not None:
            raise UserError(
                "--year is not yet supported: R5 always covers the full fixed "
                "2007-2014 range, and no specification anywhere describes what "
                "--year should do for it -- refused rather than guessed at"
            )
        if report not in _IMPLEMENTED_REPORTS:
            raise UserError(
                f"--report {report!r}: not yet implemented "
                f"(implemented so far: {', '.join(_IMPLEMENTED_REPORTS)})"
            )
        root = repo_root()
        start_ns, end_ns = stage2_descriptive_range()

        typer.echo(f"Stage 2 describe run  {_INSTRUMENT}  --report {report}")

        started = time.perf_counter()
        bars = read_bars(root, _INSTRUMENT, start_ns, end_ns)
        result = compute_r5(bars, start_ns=start_ns, end_ns=end_ns)
        md_path, parquet_path = write_r5_report(root, result)
        elapsed = time.perf_counter() - started

        _echo_summary(
            [
                "",
                f"  Bars examined      {result.bars_examined}",
                f"  Hour-of-week rows  {len(result.by_hour_of_week)}",
                f"  Year rows          {len(result.by_year)}",
                f"  Report             {md_path}",
                f"  Sidecar            {parquet_path}",
                f"  Elapsed            {_format_elapsed(elapsed)}",
            ]
        )
    except JarvisError as exc:
        typer.echo(f"jarvis describe run: {exc}")
        raise typer.Exit(code=exc.exit_code) from exc
