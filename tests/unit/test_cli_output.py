import io

import pytest
import typer

from jarvis.cli.main import _echo_summary
from jarvis.core.errors import OutputError


def test_echo_summary_prints_non_ascii_cleanly_on_cp1252_style_stream(monkeypatch):
    """Regression test for WP-015/D-065: a Windows cp1252 console cannot
    encode arbitrary Unicode (e.g. U+2229 '∩' in narrowest_intersection).
    cli/main.py reconfigures sys.stdout/sys.stderr with
    errors='backslashreplace' at import time precisely so this doesn't
    crash; this test reproduces that exact stream shape directly (rather
    than relying on the already-executed import-time side effect) and
    confirms _echo_summary prints through it without raising."""
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="cp1252", errors="strict")
    stream.reconfigure(errors="backslashreplace")
    monkeypatch.setattr("sys.stdout", stream)

    _echo_summary(["  Narrowest          C-D∩C-C"])
    stream.flush()

    written = buffer.getvalue().decode("cp1252")
    assert "C-D" in written
    assert "C-C" in written
    assert "∩" not in written
    assert "\\u2229" in written


def test_echo_summary_wraps_any_output_failure_as_output_error(monkeypatch):
    """Any failure at the output stage -- not just an encoding error --
    must surface as the documented OutputError (exit code 4), never as an
    unhandled exception: by the time _echo_summary runs, the underlying
    computation and any report file have already completed successfully."""

    def _boom(_line):
        raise RuntimeError("simulated output failure")

    monkeypatch.setattr(typer, "echo", _boom)

    with pytest.raises(OutputError) as exc_info:
        _echo_summary(["some line"])

    assert exc_info.value.exit_code == 4
