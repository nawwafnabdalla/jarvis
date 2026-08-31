"""jarvis CLI entry point."""

import importlib.metadata
import platform
import shutil
import sys
import time
from datetime import datetime, timezone

import polars as pl
import typer

from jarvis.bars.resample import resample_range
from jarvis.bars.store import read_bars
from jarvis.core.config import load_instruments, load_periods, repo_root
from jarvis.core.errors import ConfigError, JarvisError, UserError
from jarvis.core.hashing import sha256_file
from jarvis.core.types import Nanos
from jarvis.features import REGISTRY, compute, write_features
from jarvis.ingest.fetch import ingest_range
from jarvis.ingest.urls import NS_PER_HOUR
from jarvis.qa.report import run_checks, write_report
from jarvis.sessions import load_session_set

app = typer.Typer(name="jarvis")

_EXPECTED_DIRS = ("config", "src", "tests", "data", "ledger")
_DIRS_EXPECTED_MISSING = {"data": "WP-001", "ledger": "WP-00x"}


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
    except ConfigError as exc:
        typer.echo(f"jarvis doctor: {exc}")
        raise typer.Exit(code=1) from exc

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

    typer.echo("jarvis doctor")
    typer.echo(f"  Python        {py_version}  ({py_impl}, {sys.platform})")
    typer.echo(f"  Platform      {plat}")
    typer.echo(f"  Repo root     {root}")
    typer.echo(f"  tzdata        {tzdata_version}")
    typer.echo(f"  Deps lock     {lock_hash}  ({lock_name})")
    typer.echo(f"  Free disk     {free_gb:.1f} GB on {root.drive or root.anchor}")
    typer.echo(f"  Directories   {'  '.join(dir_lines)}")
    typer.echo(f"  Config        {config_lines[0]}")
    for line in config_lines[1:]:
        typer.echo(f"                {line}")
    typer.echo(f"  Status        {'OK' if config_ok else 'FAILED'}")

    if not config_ok:
        raise typer.Exit(code=1)


data_app = typer.Typer(name="data", help="Market data ingestion and validation.")
app.add_typer(data_app, name="data")

features_app = typer.Typer(name="features", help="Feature computation.")
app.add_typer(features_app, name="features")

_INSTRUMENT = "GBPUSD"


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
    except JarvisError as exc:
        typer.echo(f"jarvis data fetch: {exc}")
        raise typer.Exit(code=exc.exit_code) from exc

    typer.echo()
    typer.echo(f"  Fetched            {report.hours_fetched}")
    typer.echo(f"  Empty (no data)    {report.hours_empty}")
    typer.echo(f"  Skipped (existing) {report.hours_skipped_existing}")
    typer.echo(f"  Missing            {report.hours_missing}")
    typer.echo(f"  Rate limited       {report.hours_rate_limited}")
    typer.echo(f"  Total bytes        {report.total_bytes:,}")
    typer.echo(f"  Elapsed            {_format_elapsed(elapsed)}")


@data_app.command("resample")
def data_resample(
    from_: str = typer.Option(..., "--from", help="ISO 8601 UTC, hour-aligned"),
    to: str = typer.Option(..., "--to", help="ISO 8601 UTC, hour-aligned, exclusive"),
    allow_incomplete: bool = typer.Option(
        False,
        "--allow-incomplete",
        help="Proceed even if some hours in range have no raw blob (a hole).",
    ),
) -> None:
    """Resample raw GBP/USD ticks for the given UTC range into 1-minute bars."""
    try:
        start_ns = _parse_iso_utc_ns(from_, option_name="--from")
        end_ns = _parse_iso_utc_ns(to, option_name="--to")
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
    except JarvisError as exc:
        typer.echo(f"jarvis data resample: {exc}")
        raise typer.Exit(code=exc.exit_code) from exc

    typer.echo()
    typer.echo(f"  Hours with data    {report.hours_with_data}")
    typer.echo(f"  Hours empty        {report.hours_empty}")
    typer.echo(f"  Hours unfetched    {report.hours_unfetched}")
    typer.echo(f"  Bars written       {report.bars_written}")
    typer.echo(f"  Minutes absent     {report.minutes_absent}")
    typer.echo(f"  Months written     {', '.join(report.months_written)}")
    typer.echo(f"  Elapsed            {_format_elapsed(elapsed)}")


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
        root = repo_root()

        hours_expected = (end_ns - start_ns) // NS_PER_HOUR
        typer.echo(
            f"Validating {_INSTRUMENT}  {from_} -> {to}  ({hours_expected} hours)"
        )

        started = time.perf_counter()
        report = run_checks(root, _INSTRUMENT, start_ns, end_ns)
        elapsed = time.perf_counter() - started
        md_path, _parquet_path = write_report(root, report)
    except JarvisError as exc:
        typer.echo(f"jarvis data validate: {exc}")
        raise typer.Exit(code=exc.exit_code) from exc

    typer.echo()
    typer.echo(f"  ERROR              {report.errors}")
    typer.echo(f"  WARNING            {report.warnings}")
    typer.echo(f"  INFO               {report.infos}")
    typer.echo(f"  Sealable           {report.sealable}")
    typer.echo(f"  Report             {md_path}")
    typer.echo(f"  Elapsed            {_format_elapsed(elapsed)}")

    if report.errors > 0:
        raise typer.Exit(code=3)


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
        root = repo_root()
        names = tuple(n.strip() for n in features.split(",")) if features else tuple(REGISTRY)

        bars_df = read_bars(root, _INSTRUMENT, start_ns, end_ns)
        if bars_df.height == 0:
            typer.echo("No bars in range; nothing to compute.")
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
    except JarvisError as exc:
        typer.echo(f"jarvis features build: {exc}")
        raise typer.Exit(code=exc.exit_code) from exc

    typer.echo()
    typer.echo(f"  Bars               {bars_df.height}")
    typer.echo(f"  Feature set        v{result.feature_set_version}")
    typer.echo(f"  Months written     {', '.join(months_written)}")
    typer.echo("  Null counts:")
    for name in result.feature_names:
        typer.echo(f"    {name:<24} {result.null_counts[name]}")
    typer.echo(f"  Elapsed            {_format_elapsed(elapsed)}")
