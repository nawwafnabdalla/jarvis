from typer.testing import CliRunner

from jarvis.cli.main import app

runner = CliRunner()


def test_no_args_prints_help_exit_0():
    """`jarvis` with no arguments prints help and exits 0.
    Regression test: Click 8.5's no_args_is_help began raising UsageError
    (exit 2) instead of exiting 0. This must be implemented explicitly in
    the CLI callback, not relied upon as a Typer/Click default, so that a
    future dependency upgrade cannot silently reintroduce exit code 2 here."""
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "Usage:" in result.output
    assert "doctor" in result.output


def test_doctor_is_a_real_subcommand():
    """`jarvis doctor` succeeds as an explicit subcommand invocation, and
    `jarvis` alone does NOT silently run doctor's body. Regression test:
    Typer auto-flattens a single-command app so the bare invocation runs
    the only command directly. This must be prevented explicitly (e.g. via
    invoke_without_command=True on a group callback), because the moment a
    second command is added, an implicit flatten would previously have
    changed `jarvis`'s no-args behavior without anyone touching that code."""
    doctor_result = runner.invoke(app, ["doctor"])
    assert doctor_result.exit_code == 0
    assert "jarvis doctor" in doctor_result.output
    assert "Repo root" in doctor_result.output

    no_args_result = runner.invoke(app, [])
    assert no_args_result.exit_code == 0
    assert "Repo root" not in no_args_result.output
    assert "Usage:" in no_args_result.output
