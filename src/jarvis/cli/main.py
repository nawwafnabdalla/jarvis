"""jarvis CLI entry point."""

import importlib.metadata
import platform
import shutil
import sys

import typer

from jarvis.core.config import load_instruments, load_periods, repo_root
from jarvis.core.errors import ConfigError
from jarvis.core.hashing import sha256_file

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
