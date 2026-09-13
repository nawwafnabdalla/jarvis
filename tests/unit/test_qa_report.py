from datetime import date, datetime, timezone
from pathlib import Path

import polars as pl
import pytest
import yaml
from typer.testing import CliRunner

from jarvis.bars.resample import resample_range
from jarvis.cli.main import app
from jarvis.core.types import Nanos
from jarvis.ingest.histdata_import import TICK_SCHEMA, write_ticks
from jarvis.qa.report import QAReport, report_path, run_checks, write_report
from jarvis.timeengine import NS_PER_HOUR, trading_day_bounds

_DEFAULT_SESSIONS = {
    "tokyo": {
        "tz": "Asia/Tokyo",
        "start": "09:00",
        "end": "15:00",
    }
}


def _write_session_set(
    repo_root: Path,
    *,
    version: int = 1,
    thin_day_threshold: float = 0.60,
    sessions: dict | None = None,
) -> None:
    config_dir = repo_root / "config" / "sessions"
    config_dir.mkdir(parents=True, exist_ok=True)
    content = {
        "session_set_id": "fx_core",
        "version": version,
        "tzdata_version_at_authoring": "2026.3",
        "fold_policy": {"ambiguous": "later", "nonexistent": "later"},
        "exclude_partial": True,
        "thin_day_threshold": thin_day_threshold,
        "sessions": sessions if sessions is not None else _DEFAULT_SESSIONS,
    }
    (config_dir / f"fx_core.v{version}.yaml").write_text(yaml.dump(content), encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway repo root, isolated for both jarvis.core.config.repo_root
    (used by load_instruments and everything qa/bars/ingest touch) and
    jarvis.sessions.definitions.repo_root specifically -- that module binds
    its own `repo_root` name at import time via `from jarvis.core.config
    import repo_root`, so patching only jarvis.core.config.repo_root does
    NOT redirect load_session_set_def (see test_sessions_definitions.py's
    own isolated_repo_root fixture, which patches the same second target)."""
    monkeypatch.setattr("jarvis.core.config.repo_root", lambda: tmp_path)
    monkeypatch.setattr("jarvis.sessions.definitions.repo_root", lambda: tmp_path)
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "instruments.yaml").write_text(
        "GBPUSD:\n  point_scale: 1.0e-5\n  digits: 5\n", encoding="utf-8"
    )
    _write_session_set(tmp_path)
    return tmp_path


def _hour_ns(y: int, mo: int, d: int, h: int) -> Nanos:
    return Nanos(int(datetime(y, mo, d, h, tzinfo=timezone.utc).timestamp()) * 1_000_000_000)


def _write_ticks(
    repo_root: Path, instrument: str, year: int, month: int, records: list[tuple]
) -> None:
    """records: (ts_utc_ns, bid, ask, bid_volume, ask_volume) tuples, in
    file order (WP-010: data/tick/ is QA's source now, not Dukascopy
    blobs). Writes via the real write_ticks so these tests exercise the
    actual merge/dedup path a real import would go through."""
    frame = pl.DataFrame(
        {
            "ts_utc_ns": [r[0] for r in records],
            "bid": [r[1] for r in records],
            "ask": [r[2] for r in records],
            "bid_volume": [r[3] for r in records],
            "ask_volume": [r[4] for r in records],
            "row_sequence": list(range(len(records))),
        },
        schema=TICK_SCHEMA,
    )
    write_ticks(repo_root, instrument, year, month, frame)


# run_checks end-to-end -----------------------------------------------------


def test_run_checks_clean_range_is_sealable(repo: Path):
    hour = _hour_ns(2024, 1, 9, 3)  # Tuesday, safely mid-week
    _write_ticks(
        repo,
        "GBPUSD",
        2024,
        1,
        [
            (hour, 0.99900, 1.00000, 1.0, 1.0),
            (hour + 60_000_000_000, 0.99910, 1.00010, 1.0, 1.0),
        ],
    )
    resample_range(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))

    report = run_checks(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))
    assert isinstance(report, QAReport)
    assert report.errors == 0
    assert report.sealable is True
    assert report.months_examined == 1
    assert report.ticks_examined == 2
    assert report.bars_examined == 2


def test_sealable_is_false_iff_at_least_one_error(repo: Path):
    """Acceptance criterion 8."""
    hour = _hour_ns(2024, 1, 9, 3)
    # Non-positive spread -> E-01, an ERROR.
    _write_ticks(repo, "GBPUSD", 2024, 1, [(hour, 1.00000, 0.99900, 1.0, 1.0)])  # ask < bid

    report = run_checks(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))
    assert report.errors >= 1
    assert report.sealable is False


def test_run_checks_does_not_raise_on_error_findings(repo: Path):
    """Must not raise on ERROR findings -- QA reports; the CLI gates."""
    hour = _hour_ns(2024, 1, 9, 3)
    _write_ticks(repo, "GBPUSD", 2024, 1, [(hour, 1.00000, 0.99900, 1.0, 1.0)])
    report = run_checks(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))
    assert report.errors >= 1  # got here without raising


def test_run_checks_missing_month_contributes_no_tick_findings_not_a_crash(repo: Path):
    """WP-010: a month with no tick Parquet file at all is resample_range's
    hole to raise on, not run_checks' -- run_checks simply examines zero
    ticks for it and continues (bar-level checks still run against
    whatever bars, if any, exist)."""
    hour = _hour_ns(2024, 1, 9, 3)
    report = run_checks(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))
    assert report.months_examined == 1
    assert report.ticks_examined == 0
    assert report.errors == 0


def test_run_checks_hour_alignment_validated(repo: Path):
    from jarvis.core.errors import UserError

    hour = _hour_ns(2024, 1, 9, 3)
    with pytest.raises(UserError):
        run_checks(repo, "GBPUSD", Nanos(hour + 1), Nanos(hour + NS_PER_HOUR))


# Acceptance criterion 4: W-05 reads thin_day_threshold from the session set ---


def _build_thin_day_scenario(repo: Path) -> tuple[Nanos, Nanos]:
    """20 baseline weekday trading days at 100 bars each, then one thin day
    at 65 bars: below a 0.60 threshold (60) it is NOT flagged; below a 0.90
    threshold (90) it IS flagged.

    All ticks are written into January's single tick file first (one
    record per minute on each day, at that day's session start), then
    resampled in a SINGLE resample_range call over the whole span --
    data/tick/ is a monthly store, so there is no equivalent to the old
    per-day blob-then-resample staging."""
    from datetime import timedelta

    def weekdays_from(start: date, n: int) -> list[date]:
        days = []
        d = start
        while len(days) < n:
            if d.isoweekday() <= 5:
                days.append(d)
            d = d + timedelta(days=1)
        return days

    days = weekdays_from(date(2024, 1, 1), 21)
    records = []
    for d in days[:20]:
        s, _e = trading_day_bounds(d)
        for m in range(100):
            records.append((s + m * 60_000_000_000, 0.99900, 1.00000, 1.0, 1.0))

    thin_day = days[20]
    s, e = trading_day_bounds(thin_day)
    for m in range(65):  # well below 100
        records.append((s + m * 60_000_000_000, 0.99900, 1.00000, 1.0, 1.0))

    _write_ticks(repo, "GBPUSD", 2024, 1, records)

    start_ns = Nanos(trading_day_bounds(days[0])[0])
    resample_range(repo, "GBPUSD", start_ns, e, allow_incomplete=True)

    return start_ns, e


def test_w05_threshold_is_read_from_session_set_not_hardcoded(repo: Path):
    """Uses a distinct session-set VERSION (not a rewrite of the same
    fx_core.v1.yaml file) for the second threshold, deliberately avoiding
    load_session_set_def's content-aware cache: two writes to the same
    path in quick succession can land on an identical (mtime_ns, size)
    cache key when the new content happens to be the same byte length
    (0.60 -> 0.90 both serialise to the same width), which would silently
    serve the stale first result. A distinct version is a real cache miss
    by construction, not a workaround for a bug in this package."""
    start_ns, end_ns = _build_thin_day_scenario(repo)

    report_default = run_checks(repo, "GBPUSD", start_ns, end_ns)
    w05_default = next((f for f in report_default.findings if f.check_id == "W-05"), None)
    assert w05_default is None  # 65 bars is not below 60% of 100

    _write_session_set(repo, version=2, thin_day_threshold=0.90)
    report_strict = run_checks(
        repo, "GBPUSD", start_ns, end_ns, session_set_version=2
    )
    w05_strict = next((f for f in report_strict.findings if f.check_id == "W-05"), None)
    assert w05_strict is not None  # 65 bars IS below 90% of 100
    assert w05_strict.count == 1


# write_report ---------------------------------------------------------------


def test_write_report_produces_markdown_and_parquet(repo: Path):
    hour = _hour_ns(2024, 1, 9, 3)
    _write_ticks(repo, "GBPUSD", 2024, 1, [(hour, 0.99900, 1.00000, 1.0, 1.0)])
    resample_range(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))

    report = run_checks(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))
    md_path, parquet_path = write_report(repo, report)

    assert md_path.is_file()
    assert parquet_path.is_file()
    assert md_path.parent == repo / "reports" / "qa"

    text = md_path.read_text(encoding="utf-8")
    assert "GBPUSD" in text
    assert f"ERROR: {report.errors}" in text
    assert f"Sealable: {report.sealable}" in text

    findings_df = pl.read_parquet(parquet_path)
    assert findings_df.height == len(report.findings)
    assert set(findings_df.columns) == {
        "check_id",
        "check_name",
        "severity",
        "year",
        "count",
        "detail",
        "sample",
    }


def test_report_path_matches_naming_convention(repo: Path):
    hour = _hour_ns(2024, 1, 9, 3)
    _write_ticks(repo, "GBPUSD", 2024, 1, [(hour, 0.99900, 1.00000, 1.0, 1.0)])
    report = run_checks(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))
    generated = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    path = report_path(repo, report, generated)
    assert path.name == "QA__GBPUSD__20240109_20240109__20240601T120000Z.md"


# CLI --------------------------------------------------------------------


def test_cli_exit_code_zero_when_no_errors(repo: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("jarvis.cli.main.repo_root", lambda: repo)
    hour = _hour_ns(2024, 1, 9, 3)
    _write_ticks(repo, "GBPUSD", 2024, 1, [(hour, 0.99900, 1.00000, 1.0, 1.0)])
    resample_range(repo, "GBPUSD", hour, Nanos(hour + NS_PER_HOUR))

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "data",
            "validate",
            "--from",
            "2024-01-09T03:00:00+00:00",
            "--to",
            "2024-01-09T04:00:00+00:00",
        ],
    )
    assert result.exit_code == 0
    assert "ERROR              0" in result.output


def test_cli_exit_code_three_when_error_present(repo: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("jarvis.cli.main.repo_root", lambda: repo)
    hour = _hour_ns(2024, 1, 9, 3)
    _write_ticks(repo, "GBPUSD", 2024, 1, [(hour, 1.00000, 0.99900, 1.0, 1.0)])  # ask < bid -> E-01

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "data",
            "validate",
            "--from",
            "2024-01-09T03:00:00+00:00",
            "--to",
            "2024-01-09T04:00:00+00:00",
        ],
    )
    assert result.exit_code == 3
